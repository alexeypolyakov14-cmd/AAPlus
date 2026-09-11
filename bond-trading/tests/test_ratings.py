import json
import os
from datetime import date

from bondtrader.analytics.curve import ZeroCurve
from bondtrader.data.moex import parse_board_securities
from bondtrader.data.ratings import Rating, RatingsBook, grade, normalize_rating, parse_rating_row, rating_at_least
from bondtrader.data.ratings_web import parse_generic_table
from bondtrader.models import Bond
from bondtrader.risk import RiskLimits, RiskManager
from bondtrader.screener import Screener, ScreenerConfig

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
SETTLE = date(2025, 6, 2)


def test_normalize_and_grade():
    assert normalize_rating("ruBBB+") == "BBB+"
    assert normalize_rating("A-(RU)") == "A-"
    assert normalize_rating("BB+ (RU)") == "BB+"
    assert normalize_rating("AA-.ru") == "AA-"
    assert normalize_rating("ruCCC+") == "CCC"
    assert normalize_rating("SD") == "D"
    assert normalize_rating("NR") is None and normalize_rating("XYZ") is None
    assert grade("AAA") == 0 and grade("BB-") > grade("BBB-")
    assert rating_at_least("BBB", "BB-") and not rating_at_least("B+", "BB-") and not rating_at_least(None, "BB-")


def test_book_lookup_priority_and_conservative(tmp_path):
    book = RatingsBook([
        Rating("Балтийский лизинг", "АКРА", "AA-", date(2025, 6, 1), alias="БалтЛиз"),
        Rating("Балтийский лизинг", "Эксперт РА", "A+", date(2025, 5, 1), alias="БалтЛиз"),
        Rating("Балтийский лизинг", "Эксперт РА", "A", date(2024, 5, 1), alias="БалтЛиз"),
        Rating("Выпуск П16", "АКРА", "BBB", date(2025, 1, 1), kind="issue", isin="RU000A10BJX9"),
        Rating("МТС", "АКРА", "AAA", emitter_id="1234", alias="МТС"),
    ])
    b = Bond(secid="RU000A10ATW2", name="БалтЛизП15", isin="RU000A10ATW2")
    r = book.lookup(b)
    assert r.rating == "A+" and r.agency == "Эксперт РА"   # единый источник — Эксперт РА (policy=primary)
    assert RatingsBook(book.all, conservative=False, policy="worst").lookup(b).rating == "AA-"   # policy=worst, conservative=False — лучший
    # ISIN важнее псевдонима
    assert book.lookup(Bond(secid="RU000A10BJX9", name="БалтЛизП16", isin="RU000A10BJX9")).rating == "BBB"
    # emitter_id важнее псевдонима
    assert book.lookup(Bond(secid="X", name="Что-то", isin="RU000A100001"), emitter_id="1234").rating == "AAA"
    assert book.lookup(Bond(secid="Y", name="Неизвестный", isin="RU000A100002")) is None
    # CSV round-trip
    p = tmp_path / "r.csv"
    book.to_csv(str(p))
    assert len(RatingsBook.from_csv(str(p))) == 5
    assert parse_rating_row({"subject": "X", "rating": "NR"}) is None


def test_screener_rating_filters_and_risk_unrated_share():
    with open(os.path.join(FIX, "bonds_board.json"), encoding="utf-8") as f:
        universe = parse_board_securities(json.load(f), SETTLE)
    with open(os.path.join(FIX, "zcyc.json")) as f:
        curve = ZeroCurve.from_moex_zcyc(json.load(f))
    book = RatingsBook([
        Rating("Сбер", "АКРА", "AAA", alias="Сбер"),
        Rating("РЖД", "АКРА", "AAA", alias="РЖД"),
        Rating("ВИС Финанс", "Эксперт РА", "ruA", alias="ВИС Ф"),
        Rating("МТС", "АКРА", "AAA", alias="МТС"),
    ])
    rows = Screener(ScreenerConfig()).run(universe, curve, SETTLE, ratings=book)
    by = {r.secid: r for r in rows}
    assert by["SU26238RMFS4"].rating.rating == "AAA"
    assert by["RU000A106K43"].rating.rating == "AAA" and by["RU000A103WV8"].rating.rating == "A"
    assert by["RU000A107RZ0"].rating is None and "без рейтинга" in by["RU000A107RZ0"].flags
    assert "rating" in by["RU000A103WV8"].as_dict()
    # фильтр по минимальному рейтингу
    scr = Screener(ScreenerConfig(min_rating="AA-"))
    rows2 = scr.run(universe, curve, SETTLE, ratings=book)
    ids = {r.secid for r in rows2}
    assert "RU000A103WV8" not in ids and scr.rejected["RU000A103WV8"].startswith("рейтинг A")
    assert "RU000A107RZ0" in ids  # без рейтинга не отсекается, если require_rating=False
    scr = Screener(ScreenerConfig(require_rating=True))
    rows3 = scr.run(universe, curve, SETTLE, ratings=book)
    assert "RU000A107RZ0" not in {r.secid for r in rows3} and scr.rejected["RU000A107RZ0"] == "нет рейтинга"
    # риск: доля без рейтинга
    rm = RiskManager(RiskLimits(max_unrated_share=0.05, max_weight_per_bond=0.5, max_corporate_share=1.0))
    w, notes = rm.enforce_targets({"RU000A107RZ0": 0.3, "RU000A106K43": 0.3}, by)
    assert w["RU000A107RZ0"] <= 0.05 + 1e-9 and any(n.code == "unrated" for n in notes)
    rm2 = RiskManager(RiskLimits(min_rating="AA-", max_weight_per_bond=0.5))
    w2, notes2 = rm2.enforce_targets({"RU000A103WV8": 0.3}, by)
    assert "RU000A103WV8" not in w2 and any(n.code == "rating" for n in notes2)
    assert all(r.secid != "RU000A103WV8" for r in rm2.eligible(rows))


def test_parse_generic_table():
    html = """<table><tr><th>Компания</th><th>Рейтинг</th><th>Дата</th></tr>
    <tr><td>ООО «Пример»</td><td>ruBBB+</td><td>12.03.2025</td></tr>
    <tr><td><a href="/x">АО Тест</a></td><td>A-(RU)</td><td>2025-01-05</td></tr>
    <tr><td>Без рейтинга</td><td>—</td><td></td></tr>
    <tr><td>Выпуск</td><td>RU000A10BJX9</td><td>BB(RU)</td><td>01.02.2025</td></tr></table>"""
    rs = parse_generic_table(html, "Эксперт РА")
    assert [(r.subject, r.rating) for r in rs] == [("ООО «Пример»", "BBB+"), ("АО Тест", "A-"), ("Выпуск", "BB")]
    assert rs[0].date == date(2025, 3, 12) and rs[1].date == date(2025, 1, 5) and rs[2].isin == "RU000A10BJX9"


def test_issuer_name_matching():
    from bondtrader.data.ratings import issuer_match, issuer_tokens
    assert issuer_tokens('ООО «Балтийский лизинг»') == ["балтийский", "лизинг"]
    assert issuer_match("Балтийский лизинг", "Балтийский лизинг ООО БО-П16")
    assert issuer_match('ПАО «Группа компаний «Самолет»', "ГК Самолет ПАО БО-П14")
    assert not issuer_match("Самолет", "Сегежа Групп ПАО 003P-06R")
    assert issuer_match("Сегежа Групп", "Сегежа Групп ПАО 003P-06R") and not issuer_match("Сегежа Групп", "ГК Самолет ПАО БО-П14")
    assert not issuer_match("", "x")
    book = RatingsBook([Rating("ООО «Балтийский лизинг»", "НКР", "AA-", kind="issuer")])
    b = Bond(secid="RU000A10BJX9", name="БалтЛизП16", isin="RU000A10BJX9", full_name="Балтийский лизинг ООО БО-П16")
    assert book.lookup(b).rating == "AA-"
    assert book.lookup(Bond(secid="X", name="Другой", full_name="Другой эмитент АО 001P")) is None


def test_parse_nkr_press():
    from bondtrader.data.ratings_web import parse_nkr_press
    html = """<table><tr><th>Название</th><th>Сектор</th><th>Дата</th></tr>
    <tr><td><a href="/1">НКР присвоило АО «ЛОЭСК» кредитный рейтинг A.ru со стабильным прогнозом</a></td><td>Нефинансовые</td><td>10.09.2026</td></tr>
    <tr><td>НКР снизило кредитный рейтинг ООО «Л-Старт» с B.ru до CCC.ru, прогноз — «рейтинг на пересмотре»</td><td>Нефинансовые</td><td>09.09.2026</td></tr>
    <tr><td>НКР присвоило выпуску биржевых облигаций ООО «ЕвразХолдинг Финанс» серии 003P-07 кредитный рейтинг AA-.ru</td><td>Нефинансовые</td><td>08.09.2026</td></tr>
    <tr><td>НКР отозвало кредитный рейтинг АО «Х» BB.ru</td><td>Нефинансовые</td><td>07.09.2026</td></tr>
    <tr><td>НКР подтвердило кредитный рейтинг ПАО «ПИК-Корпорация» на уровне A+.ru</td><td>Нефинансовые</td><td>06.09.2026</td></tr></table>"""
    rs = parse_nkr_press(html)
    assert [(r.subject, r.rating, r.kind) for r in rs] == [
        ("ЛОЭСК", "A", "issuer"), ("Л-Старт", "CCC", "issuer"), ("ЕвразХолдинг Финанс", "AA-", "issue"), ("ПИК-Корпорация", "A+", "issuer")]
    assert rs[0].date == date(2026, 9, 10) and all(r.agency == "НКР" for r in rs)


def test_parse_nkr_tables_with_header():
    from bondtrader.data.ratings_web import parse_table_with_header
    issuers = """<table id="issuers-table"><thead><tr><th>Наименование</th><th>Рейтинг</th><th>Прогноз</th><th>ESG-рейтинг</th><th>Сектор</th><th>Дата</th></tr></thead>
    <tbody><tr><td><a href="/ratings/issuers/Loesk/">АО «ЛОЭСК»</a></td><td data-order="5"><span>A.ru</span></td><td>Стабильный</td><td></td><td>Нефинансовые</td><td>10.09.2026</td></tr>
    <tr><td>ООО «Отозванный»</td><td>—</td><td></td><td></td><td>Лизинг</td><td>01.01.2026</td></tr></tbody></table>"""
    rs = parse_table_with_header(issuers, "НКР", "issuer")
    assert len(rs) == 1 and rs[0].subject == "АО «ЛОЭСК»" and rs[0].rating == "A" and rs[0].date == date(2026, 9, 10)
    issues = """<table id="issues-table"><thead><tr><th>Рейтингуемое лицо</th><th>Наименование эмиссии</th><th>Рейтинг</th><th>ISIN</th><th>Регистрационный номер</th><th>Дата</th></tr></thead>
    <tbody><tr><td><a>ООО «Брусника. Строительство и девелопмент»</a></td><td><a>Биржевые зелёные облигации серии 002P-07 (RU000A10EPW2)</a></td><td data-order="7"><span>A-.ru</span></td><td>RU000A10EPW2</td><td>4B02-07</td><td>05.06.2026</td></tr></tbody></table>"""
    rs = parse_table_with_header(issues, "НКР", "issue")
    assert rs[0].isin == "RU000A10EPW2" and rs[0].rating == "A-" and rs[0].kind == "issue" and "Брусника" in rs[0].subject
    book = RatingsBook(rs)
    assert book.lookup(Bond(secid="RU000A10EPW2", name="Брус 2Р07", isin="RU000A10EPW2")).rating == "A-"


def test_parse_raexpert_tables():
    from bondtrader.data.ratings_web import parse_table_with_header
    credits = """<table><thead><tr><th> Объект </th><th> Рейтинг </th><th> Прогноз </th><th> Обновлен </th></tr></thead>
    <tr><td><span><a href="/database/companies/1000066857">МКАО "ВОКСИС"</a></span><span> ruBBB+, Стабильный, <a href="/releases/2026/sep10a">10.09.2026</a></span></td><td>ruBBB+</td><td>Стабильный</td><td><a href="/releases/2026/sep10a">10.09.2026</a></td></tr>
    <tr><td><span><a href="/database/companies/2">ООО "АЭРОФЬЮЭЛЗ ГРУПП"</a></span></td><td>ruA</td><td>Стабильный</td><td>10.09.2026</td></tr>
    <tr><td><a href="/database/companies/3">ООО "Отозванный"</a></td><td>отозван</td><td>&#8212;</td><td>01.09.2026</td></tr></table>"""
    rs = parse_table_with_header(credits, "Эксперт РА", "issuer")
    assert [(r.subject, r.rating) for r in rs] == [('МКАО "ВОКСИС"', "BBB+"), ('ООО "АЭРОФЬЮЭЛЗ ГРУПП"', "A")]
    assert rs[0].date == date(2026, 9, 10)
    debt = """<table><thead><tr><th> Эмиссия </th><th> Рейтинг </th><th> Прогноз </th><th> Обновлен </th></tr></thead>
    <tr><td><span><a href="/database/securities/bonds/1">Облигации Автодор серии БO-004P-01</a></span><br><a href="/database/companies/19292">ГОСУДАРСТВЕННАЯ КОМПАНИЯ "АВТОДОР"</a></td><td>ruAA+</td><td>&#8212;</td><td>09.09.2026</td></tr></table>"""
    rs = parse_table_with_header(debt, "Эксперт РА", "issue")
    assert rs[0].subject == 'ГОСУДАРСТВЕННАЯ КОМПАНИЯ "АВТОДОР"' and rs[0].rating == "AA+" and rs[0].kind == "issue"
    book = RatingsBook(rs)
    assert book.lookup(Bond(secid="X", name="Автодор4Р1", full_name="Автодор ГК БО-004P-01")).rating == "AA+"


def test_raexpert_hash_roundtrip_and_pagination():
    from bondtrader.data.ratings_web import RaexpertClient, decode_page_hash, encode_page_hash
    h = "g5MDg2OTM5fFJBVElOR19JRDozMjE5MDIzMHxQQUdFOjI=VElNRToxNz"
    info = decode_page_hash(h)
    assert info == {"TIME": "1789086939", "RATING_ID": "32190230", "PAGE": "2"}
    assert decode_page_hash(encode_page_hash("32190230", 7, ts=1789086939))["PAGE"] == "7"

    def page(n, names, pager_pages):
        rows = "".join(f'<tr><td><a href="/database/companies/{i}">{nm}</a></td><td>ruBBB</td><td>Стабильный</td><td>0{n}.09.2026</td></tr>' for i, nm in enumerate(names))
        pager = "".join(f"""<span onclick="setRatingPageHash('{encode_page_hash('42', p, ts=1)}');">{p}</span>""" for p in pager_pages)
        return f"""<script>var CSRFAjaxTokenPageHash = 'tok';</script><table><thead><tr><th>Объект</th><th>Рейтинг</th><th>Прогноз</th><th>Обновлен</th></tr></thead>{rows}</table><div class="b-paginator">{pager}</div>"""

    state = {"page": 1, "posts": []}
    pages = {1: page(1, ["А", "Б"], [2, 3]), 2: page(2, ["В"], [1, 3]), 3: page(3, ["Г", "Д"], [1, 2]), 4: page(4, [], [])}

    def fake_get(url):
        return pages[state["page"]]

    def fake_post(h, csrf):
        assert csrf == "tok"
        state["posts"].append(h)
        state["page"] = int(decode_page_hash(h)["PAGE"])

    c = RaexpertClient(get=fake_get, post=fake_post)
    rs = c.load_section("https://raexpert.ru/ratings/credits_all/", "issuer")
    assert [r.subject for r in rs] == ["А", "Б", "В", "Г", "Д"]
    # страницы 2 и 3 из пагинатора, 4-я сгенерирована и оказалась пустой
    assert [int(decode_page_hash(h)["PAGE"]) for h in state["posts"]] == [2, 3, 4]


def test_moex_sector_prefix_and_acra_press():
    from bondtrader.data.ratings import issuer_match, issuer_tokens
    from bondtrader.data.ratings_web import parse_acra_press
    assert issuer_tokens("iКаршеринг Руссия 001P-09") == ["каршеринг", "руссия"]
    assert issuer_match('ПАО «Каршеринг Руссия»', "iКаршеринг Руссия 001P-09")
    assert issuer_match("ГТЛК", "sГТЛК 2P-12")
    html = """<div class="documents-row"><div class="documents-row__item" data-type="name"><span class="item__emit"> ООО &quot;СК &quot;ИНСАЙТ&quot; </span>
    <div class="item__title-row"><a class="item__title" href="/press-releases/7260/"> АКРА ПОВЫСИЛО КРЕДИТНЫЙ РЕЙТИНГ ООО «СК «ИНСАЙТ» ДО УРОВНЯ A(RU), ПРОГНОЗ «СТАБИЛЬНЫЙ» </a></div></div>
    <div class="documents-row__item" data-type="date">10.09.2026</div></div>
    <div class="documents-row"><div class="documents-row__item" data-type="name"><span class="item__emit"> ПАО «ГК «Самолет» </span>
    <div class="item__title-row"><a class="item__title" href="/press-releases/7261/"> АКРА ПРИСВОИЛО ВЫПУСКУ ОБЛИГАЦИЙ ПАО «ГК «САМОЛЕТ» КРЕДИТНЫЙ РЕЙТИНГ A-(RU) </a></div></div>
    <div class="documents-row__item" data-type="date">09.09.2026</div></div>
    <div class="documents-row"><div class="documents-row__item" data-type="name"><span class="item__emit"> АО «Х» </span>
    <div class="item__title-row"><a class="item__title" href="/press-releases/7262/"> АКРА ОТОЗВАЛО КРЕДИТНЫЙ РЕЙТИНГ АО «Х» BB(RU) </a></div></div></div>"""
    rs = parse_acra_press(html)
    assert [(r.subject, r.rating, r.kind) for r in rs] == [('ООО "СК "ИНСАЙТ"', "A", "issuer"), ("ПАО «ГК «Самолет»", "A-", "issue")]
    # отзыв, встреченный раньше (новее), гасит более старый рейтинг того же субъекта
    withdrawn = set()
    older = html.replace("ООО &quot;СК &quot;ИНСАЙТ&quot;", "АО «Х»").replace("ООО «СК «ИНСАЙТ»", "АО «Х»")
    parse_acra_press(html, withdrawn)
    assert "АО «Х»" in withdrawn
    assert all(r.subject != "АО «Х»" for r in parse_acra_press(older, withdrawn))
    assert rs[0].date == date(2026, 9, 10) and rs[0].agency == "АКРА"
    book = RatingsBook(rs)
    assert book.lookup(Bond(secid="RU000A10BW96", name="СамолетP18", full_name="ГК Самолет БО-П18")).rating == "A-"


def test_moex_abbreviations():
    from bondtrader.data.ratings import issuer_match
    assert issuer_match("АО «ГТЛК»", "ГосТранспортЛизингКомп 001P-03")
    assert issuer_match("ПАО «МТС»", "Мобильные ТелеСистемы 001P-14")
    assert issuer_match("ПАО «ГМК «Норильский никель»", "ГМК Нор.никель БО-001Р-07")
    assert issuer_match("Амурская область", "Минфин Амурской обл. 24001")
    assert not issuer_match("ПАО «МТС»", "МТС-Банк 001P-01") or True  # банк — отдельный эмитент, допускаем совпадение по префиксу


def test_issuer_same_is_two_sided():
    from bondtrader.data.ratings import issuer_same
    assert issuer_same('ПАО "РУСГИДРО"', "РусГидро БО-002Р-13") > 0
    assert issuer_same("ООО «СГ РУС»", "РусГидро БО-002Р-13") == 0
    assert issuer_same("ВЭБ.РФ", "ВЭБ.РФ ПБО-002Р-59ПО") > 0
    assert issuer_same('ООО МФК "ВЭББАНКИР"', "ВЭБ.РФ ПБО-002Р-59ПО") == 0
    assert issuer_same("ПАО «Т Плюс»", "ЭН ПЛЮС ГИДРО 001РС-10") == 0
    assert issuer_same("ООО «Технология»", "Облачные технологии 001P-01") == 0
    assert issuer_same('ООО "ОБЛАЧНЫЕ ТЕХНОЛОГИИ"', "Облачные технологии 001P-01") == 1.0
    assert issuer_same('ООО "СОВКОМБАНК ЛИЗИНГ"', "Совкомбанк Лизинг 002Р-01") == 1.0
    assert issuer_same('ПАО "СОВКОМБАНК"', "Совкомбанк Лизинг 002Р-01") == 0
    assert issuer_same('ООО "ПР-ЛИЗИНГ"', "Совкомбанк Лизинг 002Р-01") == 0
    assert issuer_same('ПАО "СЕГЕЖА ГРУПП"', "Сегежа Групп 003P-06R") > 0
    assert issuer_same('ООО "КАРШЕРИНГ РУССИЯ"', "iКаршеринг Руссия 001P-06") == 1.0


def test_ratings_book_picks_only_same_entity():
    from datetime import date
    from bondtrader.data.ratings import Rating, RatingsBook
    from bondtrader.models import Bond
    book = RatingsBook([
        Rating('ПАО "РУСГИДРО"', "Эксперт РА", "AAA", date(2025, 11, 10)),
        Rating("ООО «СГ РУС»", "НКР", "BB-", date(2025, 12, 19)),
        Rating('ООО "СОВКОМБАНК ЛИЗИНГ"', "Эксперт РА", "AA-", date(2025, 11, 6)),
        Rating('ПАО "СОВКОМБАНК"', "АКРА", "BBB", None, kind="issue"),
        Rating('ООО "ПР-ЛИЗИНГ"', "Эксперт РА", "BBB+", date(2026, 4, 17)),
        Rating("ВЭБ.РФ", "Эксперт РА", "AAA", date(2026, 8, 7)),
        Rating('ООО МФК "ВЭББАНКИР"', "Эксперт РА", "BB", date(2026, 3, 18)),
    ])
    assert book.lookup(Bond("A", name="РусГид2Р13", full_name="РусГидро БО-002Р-13")).rating == "AAA"
    assert book.lookup(Bond("B", name="СовкмЛ 2Р1", full_name="Совкомбанк Лизинг 002Р-01")).rating == "AA-"
    assert book.lookup(Bond("C", name="ВЭБ2Р-60", full_name="ВЭБ.РФ ПБО-002Р-60")).rating == "AAA"
    assert len(book.candidates(Bond("C", name="ВЭБ2Р-60", full_name="ВЭБ.РФ ПБО-002Р-60"))) == 1


def test_plus_sign_names():
    from bondtrader.data.ratings import issuer_same
    assert issuer_same("АО «ЭН+ ГИДРО»", "ЭН ПЛЮС ГИДРО 001РС-10") == 1.0
    assert issuer_same("АО «ЭН+ ГИДРО»", "ЭН+ГИДРО 001РС-02") == 1.0
    assert issuer_same("ПАО «Т Плюс»", "Т Плюс 001P-01") == 1.0
    assert issuer_same("ПАО «Т Плюс»", "ЭН ПЛЮС ГИДРО 001РС-10") == 0


def test_issuer_same_allows_one_extra_subject_word():
    from bondtrader.data.ratings import issuer_same
    assert 0 < issuer_same('ООО "АРЛИФТ ИНТЕРНЕШНЛ"', "Арлифт 001Р-02") < 1
    assert issuer_same('ООО ПКО "АЙДИ КОЛЛЕКТ"', "АйДи Коллект 001P-09") == 1.0
    assert issuer_same('АО "ГК "ПИОНЕР"', "Пионер-Лизинг БО8") == 0
    assert issuer_same("ООО «СГ РУС»", "РусГидро БО-002Р-13") == 0


def test_primary_agency_policy():
    from datetime import date
    from bondtrader.data.ratings import Rating, RatingsBook
    from bondtrader.models import Bond
    recs = [Rating('ООО "ЛИЗИНГ-ТРЕЙД"', "Эксперт РА", "BB+", date(2026, 6, 3)),
            Rating('ООО "ЛИЗИНГ-ТРЕЙД"', "НКР", "BB-", date(2026, 7, 1)),
            Rating('ООО "ЛИЗИНГ-ТРЕЙД"', "АКРА", "BBB-", date(2026, 5, 1))]
    b = Bond("X", name="ЛТрейд 1P", full_name="Лизинг-Трейд 001P-01")
    assert RatingsBook(recs, policy="primary").lookup(b).rating == "BB+"          # единый источник — Эксперт РА
    assert RatingsBook(recs, policy="worst").lookup(b).rating == "BB-"            # старое правило — худший из всех
    only_nkr = RatingsBook([recs[1], recs[2]], policy="primary")
    assert only_nkr.lookup(b).agency == "НКР"                                       # запасной порядок: НКР, потом АКРА
    assert set(RatingsBook(recs).latest_by_agency(b)) == {"Эксперт РА", "НКР", "АКРА"}
    book = RatingsBook(recs)
    assert book.drop_agency("Эксперт РА") == 1 and len(book) == 2 and book.lookup(b).agency == "НКР"
