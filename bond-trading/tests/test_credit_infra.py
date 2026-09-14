"""Инфраструктура кредитного анализа: ГИР БО, книги отчётности/событий/новостей, секторы, справедливый спред, value_hy."""
import json
import os
from datetime import date, timedelta

import pytest

from bondtrader.analytics.curve import ZeroCurve
from bondtrader.analytics.fair_spread import features_of, fit_fair_spread
from bondtrader.data.disclosure import DisclosureEvent, EventsBook, classify, parse_events_html, parse_search_html
from bondtrader.data.financials import FinancialsBook, IssuerMap, IssuerRecord, Statement, inn_from_description
from bondtrader.data.girbo import parse_details
from bondtrader.data.moex import parse_board_securities
from bondtrader.data.news import NewsBook, NewsItem, parse_rss, score_title
from bondtrader.data.sectors import sector_of
from bondtrader.models import Bond
from bondtrader.portfolio import Portfolio
from bondtrader.risk import RiskLimits, RiskManager
from bondtrader.screener import Screener, ScreenerConfig
from bondtrader.strategies import MarketContext, make_strategy

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
SETTLE = date(2025, 6, 2)


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- ГИР БО

def test_girbo_parse_details_scales_by_okei():
    payload = [{"id": 1, "correctionVersion": 0, "period": "2024",
                "balance": {"okei": "385", "current1300": 12.5, "previous1300": 10.0, "current1600": 100, "current1510": 20, "current1410": 30, "current1250": 5},
                "financialResult": {"current2110": 80, "previous2110": 70, "current2200": 9, "current2330": -3, "current2400": 4}}]
    st = parse_details(payload, "7700000000", 2024)
    assert st is not None and st.year == 2024 and st.source == "girbo"
    assert st.v("1300") == 12500 and st.v("2110") == 80000 and st.previous["2110"] == 70000   # млн -> тыс.
    prev = st.prev_statement()
    assert prev is not None and prev.year == 2023 and prev.v("1300") == 10000
    assert parse_details({"foo": "bar"}, "1", 2024) is None


def test_inn_from_moex_description():
    assert inn_from_description({"SECID": "X", "INN": "7826705374"}) == "7826705374"
    assert inn_from_description({"SECID": "X", "REGNUMBER": "4B02"}) is None


# ---------------------------------------------------------------- книги отчётности и ИНН

def test_financials_book_roundtrip_and_metrics(tmp_path):
    book = FinancialsBook()
    book.add(Statement("111", 2023, {"2110": 9000, "1300": 1000, "2200": 500, "2330": 100}))
    book.add(Statement("111", 2024, {"2110": 10000, "1300": 1200, "2200": 800, "2330": 200, "1410": 1000, "1510": 200, "1250": 300, "1400": 1000, "1500": 900, "1200": 2000}))
    book.add(Statement("222", 2024, {"2110": 100, "2200": -50, "1300": -10, "1510": 900, "2330": 300}, previous={"2110": 500}))
    p = tmp_path / "fin.csv"
    book.to_csv(str(p))
    loaded = FinancialsBook.from_csv(str(p))
    assert len(loaded) == 3 and loaded.issuers == 2
    m = loaded.metrics("111")
    assert m.year == 2024 and m.interest_coverage == 4.0 and m.score >= 90 and not m.flags
    bad = loaded.metrics("222")
    assert bad.year == 2024 and "отрицательный капитал" in bad.flags and any("выручка" in f for f in bad.flags)  # previous из той же формы
    assert loaded.metrics("333") is None


def test_issuer_map_lookup_by_isin_alias_and_name():
    m = IssuerMap([IssuerRecord("1", "Балтийский лизинг", alias="БалтЛиз", sector="leasing"),
                   IssuerRecord("2", "МТС", isin="RU000A104ZK2"),
                   IssuerRecord("3", "Группа компаний Самолет")])
    assert m.lookup(Bond("A", name="БалтЛизП16", full_name="Балтийский лизинг ООО БО-П16")).inn == "1"
    assert m.lookup(Bond("RU000A104ZK2", name="МТС 1P-21", isin="RU000A104ZK2")).inn == "2"
    assert m.lookup(Bond("B", name="Самолет1P12", full_name="ГК Самолет ПАО БО-П12")).inn == "3"
    assert m.lookup(Bond("C", name="Неизвестный", full_name="ООО Неизвестный завод")) is None


# ---------------------------------------------------------------- e-disclosure

def test_disclosure_classify_and_parse():
    assert classify("Неисполнение обязательств эмитента по выплате купонного дохода") == "default"
    assert classify("О технической ошибке") == "other"
    assert classify("Технический дефолт по облигациям серии БО-01") == "tech_default"
    assert classify("Об определении размера процентной ставки по купонному периоду") == "coupon"
    assert classify("О присвоении кредитного рейтинга") == "rating"
    assert classify("Раскрытие годовой бухгалтерской отчетности") == "report"
    html = """
    <table><tr><td>15.03.2025 10:00</td><td><a href="/portal/event.aspx?EventId=100">Об определении ставки купона</a></td></tr>
    <tr><td>02.04.2025</td><td><a href="/portal/event.aspx?EventId=101">Неисполнение обязательств эмитента по выплате купона</a></td></tr>
    <tr><td>10.04.2025</td><td><a href="/portal/event.aspx?EventId=102">Технический дефолт по погашению части номинала</a></td></tr></table>
    """
    evs = parse_events_html(html, issuer="ООО Ромашка", company_id="777")
    assert [e.kind for e in evs] == ["coupon", "default", "tech_default"]
    assert evs[1].date == date(2025, 4, 2) and evs[1].url.endswith("EventId=101") and evs[1].company_id == "777"
    found = parse_search_html('<tr><td><a href="/portal/company.aspx?id=555">ООО «Ромашка»</a></td><td>7700000001</td></tr>')
    assert found == [{"id": "555", "name": "ООО «Ромашка»", "inn": "7700000001"}]


def test_events_book_stop_factors(tmp_path):
    book = EventsBook()
    assert book.add(DisclosureEvent(date(2025, 4, 2), "ООО Ромашка", "default", "Неисполнение обязательств", inn="1"))
    assert not book.add(DisclosureEvent(date(2025, 4, 2), "ООО Ромашка", "default", "Неисполнение обязательств", inn="1"))
    book.add(DisclosureEvent(date(2023, 1, 1), "ООО Ромашка", "tech_default", "старый", inn="1"))
    book.add(DisclosureEvent(date(2025, 5, 1), "ООО Ромашка", "coupon", "ставка", inn="1"))
    stops = book.stop_factors(SETTLE, 365, inn="1")
    assert len(stops) == 1 and stops[0].kind == "default"
    assert book.stop_factors(SETTLE, 365, name="Ромашка ООО БО-01") and not book.stop_factors(SETTLE, 365, name="Лютик")
    p = tmp_path / "ev.csv"
    book.to_csv(str(p))
    assert len(EventsBook.from_csv(str(p))) == 3


# ---------------------------------------------------------------- секторы

def test_sectors():
    assert sector_of("БалтЛизП16", "Балтийский лизинг ООО") == "leasing"
    assert sector_of("МВ Финанс 1P4", "МФК Быстроденьги") == "mfo"
    assert sector_of("Самолет1P12", "ГК Самолет ПАО") == "real_estate"
    assert sector_of("ОФЗ 26238") == "gov"
    assert sector_of("Сбер Sb42R", "Сбербанк ПАО") == "bank"
    assert sector_of("Нечто", "") == "other"


# ---------------------------------------------------------------- новости

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>
<item><title>ООО Ромашка допустила дефолт по облигациям - Интерфакс</title><link>http://x/1</link><pubDate>Mon, 26 May 2025 10:00:00 GMT</pubDate><source url="http://interfax.ru">Интерфакс</source></item>
<item><title>АКРА повысило рейтинг ООО Ромашка до BBB(RU) - РБК</title><link>http://x/2</link><pubDate>Thu, 01 May 2025 10:00:00 GMT</pubDate></item>
<item><title>Ромашка разместила облигации на 500 млн, ставка купона 22% - Cbonds</title><link>http://x/3</link><pubDate>Thu, 01 May 2025 10:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_news_parse_score_and_decay(tmp_path):
    items = parse_rss(RSS, "google")
    assert len(items) == 3 and items[0][0] == date(2025, 5, 26) and items[0][3] == "Интерфакс"
    assert score_title(items[0][1])[0] <= -4 and "default" in score_title(items[0][1])[1]
    assert score_title(items[1][1])[0] > 0 and score_title(items[2][1]) == (0.0, ["routine"])
    # ложные срабатывания: «выпуск» ≠ СК, «искусство» ≠ иск; блоги и вопросы не считаются
    assert score_title("Автобан погасил пятилетний выпуск облигаций")[1] == ["paid"]
    assert score_title("Бобровский — эталон вратарского искусства") == (0.0, [])
    assert "lawsuit" in score_title("ГТЛК подала иск к EasyJet")[1] and "criminal" not in score_title("ГТЛК подала иск к EasyJet")[1]
    assert score_title("Балтийский лизинг: сможет ли он расплатиться?")[1][-1] == "blog"
    assert score_title("Дефолт близко", "google:Smart-Lab") == (0.0, ["default", "blog"])
    assert "criminal" in score_title("СК возбудил дело против гендиректора")[1]
    book = NewsBook()
    for d, title, link, _ in items:
        sc, tags = score_title(title)
        book.add(NewsItem(d, "Ромашка", "google", title, link, "", sc, ",".join(tags)))
    ns = book.issuer_score(SETTLE, name="Ромашка ООО БО-01", days=90)
    assert ns.n == 3 and ns.negative == 1 and ns.positive == 1 and ns.worst.date == date(2025, 5, 26)
    assert ns.stop is not None and ns.stop.tags.startswith("default")
    assert -4.0 < ns.score < -1.0     # затухание: свежий дефолт (−4·0.85) + старый плюс (2·0.48)
    far = book.issuer_score(SETTLE + timedelta(days=400), name="Ромашка", days=90)
    assert far.n == 0 and far.score == 0
    p = tmp_path / "news.csv"
    book.to_csv(str(p))
    assert len(NewsBook.from_csv(str(p))) == 3


# ---------------------------------------------------------------- справедливый спред

def _row(secid, grade_rating, dur, turnover, spread, level=2, ofz=False):
    from bondtrader.data.ratings import Rating
    from bondtrader.models import BondMetrics, Quote
    from bondtrader.screener import ScreenRow
    b = Bond(secid, name=secid, list_level=level)
    q = Quote(secid, SETTLE, price=100, turnover=turnover, bid=99.8, ask=100.2)
    m = BondMetrics(secid, 100, 1000, 20.0, None, 20.0, dur, dur / 1.2, 0, 0.1, dur, 15, spread)
    return ScreenRow(b, q, m, rating=Rating("x", "y", grade_rating) if grade_rating else None)


def test_fair_spread_model_recovers_structure():
    rows = []
    grades = ["A", "BBB", "BB", "B"]
    for i in range(40):
        g = grades[i % 4]
        dur = 0.5 + (i % 5) * 0.5
        from bondtrader.data.ratings import GRADE
        spread = 100 + 60 * GRADE[g] + 20 * dur + (5 if i % 7 == 0 else -5)
        rows.append(_row(f"S{i}", g, dur, 5e6, spread))
    model = fit_fair_spread(rows)
    assert model.ok and model.r2 > 0.95
    assert 50 < model.coef["grade"] < 70 and 10 < model.coef["duration"] < 30
    cheap = _row("CHEAP", "BB", 1.0, 5e6, 100 + 60 * 11 + 20 + 300)
    assert model.residual(features_of(cheap), cheap.metrics.g_spread) > 250
    assert not fit_fair_spread(rows[:5]).ok


# ---------------------------------------------------------------- value_hy + стоп-факторы + секторный лимит

@pytest.fixture
def universe():
    return parse_board_securities(load("bonds_board.json"), SETTLE)


@pytest.fixture
def curve():
    return ZeroCurve.from_moex_zcyc(load("zcyc.json"))


def test_screener_uses_books_and_value_hy_ranks(universe, curve):
    issuers = IssuerMap([IssuerRecord("1", "ВИС Финанс", alias="ВИС Ф", sector="finance"), IssuerRecord("2", "Газпром нефть", alias="Газпнф"), IssuerRecord("3", "Сбербанк", alias="Сбер")])
    fin = FinancialsBook([
        Statement("1", 2024, {"2110": 1000, "2200": 300, "1300": 500, "1410": 400, "1510": 100, "2330": 50, "1250": 100, "1400": 400, "1500": 300, "1200": 600}),
        Statement("2", 2024, {"2110": 1000, "2200": -10, "1300": -5, "1510": 900, "2330": 300}),   # отрицательный капитал -> стоп
    ])
    events = EventsBook([DisclosureEvent(date(2025, 5, 20), "Сбербанк ПАО", "tech_default", "Технический дефолт", inn="3")])
    news = NewsBook([NewsItem(date(2025, 5, 30), "ВИС Финанс", "google", "ВИС Финанс погасила выпуск облигаций", "", "1", 1.5, "paid")])
    scr = Screener(ScreenerConfig(min_turnover=1e6, max_list_level=2, max_bid_ask_pct=1.0))
    rows = scr.run(universe, curve, SETTLE, ratings=None, financials=fin, issuers=issuers, events=events, news=news)
    ids = {r.secid: r for r in rows}
    assert "RU000A106K43" not in ids and scr.rejected["RU000A106K43"].startswith("tech_default")
    vis = ids["RU000A103WV8"]
    assert vis.inn == "1" and vis.fin is not None and vis.fin.score >= 90 and vis.sector == "finance" and vis.news.n == 1
    gpn = ids["RU000A107RZ0"]
    assert gpn.fin is not None and "отрицательный капитал" in gpn.fin.flags and any("отчётность" in f for f in gpn.flags)
    # риск: отрицательный капитал — не покупаем; сектор ограничен
    risk = RiskManager(RiskLimits(max_weight_per_bond=0.5, max_weight_per_issuer=0.5, max_corporate_share=1.0, max_sector_share=0.2))
    elig = risk.eligible(rows)
    assert "RU000A107RZ0" not in {r.secid for r in elig}
    w, notes = risk.enforce_targets({"RU000A103WV8": 0.5, "RU000A107RZ0": 0.3}, ids)
    assert "RU000A107RZ0" not in w and abs(w["RU000A103WV8"] - 0.2) < 1e-9
    assert any(n.code == "financials" for n in notes) and any(n.code == "sector_cap" for n in notes)
    # стратегия: стоп по отчётности, ОФЗ-ядро, пояснения с компонентами
    strat = make_strategy("value_hy", {"top_n": 5, "ofz_min_share": 0.1})
    ctx = MarketContext(SETTLE, rows, curve, portfolio=Portfolio())
    targets = strat.targets(ctx)
    assert "RU000A107RZ0" in strat.stops and "RU000A103WV8" in targets
    assert any(ids[s].bond.is_ofz for s in targets) and abs(sum(targets.values()) - 1.0) < 1e-9
    reason = strat.explain(ctx)["RU000A103WV8"]
    assert "композит" in reason and "балл 100" in reason and "новости +" in reason
    # новость СМИ о дефолте — стоп; та же новость из блога — нет; флаг дефолта в реестре MOEX — стоп
    bad_news = NewsBook([NewsItem(date(2025, 5, 30), "ВИС Финанс", "google:Интерфакс", "ВИС Финанс допустила дефолт", "", "1", -4.0, "default"),
                         NewsItem(date(2025, 5, 29), "Газпром нефть", "google:Smart-Lab", "Газпром нефть: дефолт?", "", "2", 0.0, "default,blog")])
    scr2 = Screener(ScreenerConfig(min_turnover=1e6, max_list_level=2, max_bid_ask_pct=1.0))
    rows2 = scr2.run(universe, curve, SETTLE, issuers=issuers, news=bad_news,
                     describe=lambda b: {"HASDEFAULT": "0", "HASTECHNICALDEFAULT": "1"} if b.secid == "RU000A104ZK2" else {})
    ids2 = {r.secid for r in rows2}
    assert "RU000A103WV8" not in ids2 and scr2.rejected["RU000A103WV8"].startswith("новости: default")
    assert "RU000A107RZ0" in ids2
    assert "RU000A104ZK2" not in ids2 and "технический дефолт" in scr2.rejected["RU000A104ZK2"]


def test_cli_books_commands(tmp_path, capsys):
    from bondtrader.cli import main
    cfg = tmp_path / "c.yaml"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "issuers.csv").write_text("inn,name,alias,isin,sector,emitter_id\n1,ВИС Финанс,ВИС Ф,,,\n", encoding="utf-8")
    (tmp_path / "data" / "financials.csv").write_text("inn,year,code,value,source,fetched\n1,2024,2110,1000,girbo,2025-06-01\n1,2024,2200,300,girbo,2025-06-01\n1,2024,1300,500,girbo,2025-06-01\n1,2024,2330,50,girbo,2025-06-01\n", encoding="utf-8")
    (tmp_path / "data" / "disclosure.csv").write_text("date,issuer,inn,company_id,kind,title,url\n2025-05-20,ВИС Финанс,1,,coupon,Ставка купона,\n", encoding="utf-8")
    (tmp_path / "data" / "news.csv").write_text("date,query,inn,source,title,url,score,tags\n2025-05-30,ВИС Финанс,1,google,Иск к ВИС Финанс,,-1.5,lawsuit\n", encoding="utf-8")
    cfg.write_text(f"data:\n  financials_csv: {tmp_path/'data'/'financials.csv'}\n  issuers_csv: {tmp_path/'data'/'issuers.csv'}\n"
                   f"  disclosure_csv: {tmp_path/'data'/'disclosure.csv'}\n  news_csv: {tmp_path/'data'/'news.csv'}\n  ratings_csv: ''\n", encoding="utf-8")
    assert main(["-c", str(cfg), "--fixtures", FIX, "financials", "coverage"]) == 0
    out = capsys.readouterr().out
    assert "с отчётностью 1" in out
    assert main(["-c", str(cfg), "financials", "show", "--query", "1"]) == 0
    assert "покрытие процентов 6.00x" in capsys.readouterr().out
    assert main(["-c", str(cfg), "disclosure", "list"]) == 0 and "coupon" in capsys.readouterr().out
    assert main(["-c", str(cfg), "news", "show", "--query", "ВИС Финанс", "--days", "100000"]) == 0
    assert "негатив 1" in capsys.readouterr().out
    assert main(["-c", str(cfg), "--fixtures", FIX, "signals", "-s", "value_hy"]) == 0
    out = capsys.readouterr().out
    assert "Отчётность: 1 эмитентов" in out and "Новости: 1 новостей" in out and "value_hy" in out


def test_structured_and_rating_cap_and_floater_after_enrich(universe, curve):
    from bondtrader.data.ratings import Rating, RatingsBook
    assert Bond("X", name="СБСекр5АС", full_name="СФО СБ Секьюритизация 5 кл.АC").is_structured
    assert Bond("Y", name="Титан-5 А", full_name="ИА Титан-5 А").is_structured
    assert Bond("Z", name="Сплит ПВ-3", full_name="СФО Сплит Финанс ПВ-3").is_structured
    assert not Bond("W", name="БалтЛизП16", full_name="Балтийский лизинг ООО БО-П16").is_structured
    book = RatingsBook([Rating("ВИС Финанс", "АКРА", "A", alias="ВИС Ф"), Rating("Газпром нефть", "АКРА", "AAA", alias="Газпнф"),
                        Rating("Сбербанк", "АКРА", "BBB", alias="Сбер")])
    scr = Screener(ScreenerConfig(min_turnover=1e6, max_list_level=2, max_bid_ask_pct=1.0, max_rating="BBB+"))
    rows = scr.run(universe, curve, SETTLE, ratings=book)
    ids = {r.secid for r in rows}
    assert "RU000A103WV8" not in ids and scr.rejected["RU000A103WV8"].startswith("рейтинг A выше")
    assert "RU000A106K43" in ids and any(r.bond.is_ofz for r in rows)   # BBB проходит, ОФЗ не режем
    # value_hy: кандидаты только ≤ max_rating, модель — по всем
    st = make_strategy("value_hy", {"top_n": 5, "max_rating": "BBB+", "ofz_min_share": 0.0})
    rows3 = Screener(ScreenerConfig(min_turnover=1e6, max_list_level=2, max_bid_ask_pct=1.0)).run(universe, curve, SETTLE, ratings=book)
    t = st.targets(MarketContext(SETTLE, rows3, curve, portfolio=Portfolio()))
    assert "RU000A106K43" in t and "RU000A103WV8" not in t and "RU000A107RZ0" not in t
    # флоатер по графику (будущие купоны неизвестны) отсеивается после enrich (enrich мутирует Bond — поэтому в конце теста)
    def enrich(b):
        if b.secid == "RU000A106K43":
            b.coupons = [(date(2025, 12, 1), 40.0), (date(2026, 6, 1), None), (date(2026, 12, 1), None), (date(2027, 6, 1), None)]
            b.has_full_schedule = True
        return b
    scr2 = Screener(ScreenerConfig(min_turnover=1e6, max_list_level=2, max_bid_ask_pct=1.0))
    rows2 = scr2.run(universe, curve, SETTLE, enrich=enrich)
    assert "RU000A106K43" not in {r.secid for r in rows2} and scr2.rejected["RU000A106K43"] == "флоатер"


def test_spreads_cli(capsys):
    from bondtrader.cli import main
    assert main(["--fixtures", FIX, "spreads", "--top", "5", "--bottom", "2"]) == 0
    out = capsys.readouterr().out
    assert "Спреды к кривой ОФЗ по ступеням" in out and "vs_peers" in out and "За что платят меньше" in out


def test_news_stop_requires_issuer_as_subject():
    from bondtrader.data.news import is_default_subject
    assert is_default_subject("ООО Ромашка допустила дефолт по облигациям", "Ромашка ООО БО-01")
    assert is_default_subject("Суд признал банкротом ООО Ромашка", "Ромашка ООО БО-01")
    assert not is_default_subject("Сбербанк банкротит бетонный завод", "Сбербанк ПАО")          # истец
    assert not is_default_subject("Рынок считает, что Ромашка обанкротится", "Ромашка ООО")     # спекуляция
    assert not is_default_subject("ООО Лютик допустил дефолт", "Ромашка ООО")                    # другой эмитент
    assert score_title("Вытегорец отсудил у «Россетей» неустойку за просрочку")[1] == ["plaintiff"]
    assert "default" not in score_title("Селектел не выплатил дивиденды за 3 месяца")[1]
    book = NewsBook([NewsItem(date(2025, 5, 30), "Сбербанк ПАО", "google:РБК", "Сбербанк банкротит бетонный завод", "", "", -0.5, "plaintiff"),
                     NewsItem(date(2025, 5, 30), "Ромашка ООО", "google:РБК", "ООО Ромашка допустила дефолт по облигациям", "", "", -4.0, "default")])
    assert book.issuer_score(SETTLE, name="Сбербанк ПАО").stop is None
    assert book.issuer_score(SETTLE, name="Ромашка ООО БО-01").stop is not None


def test_fundamentals_debt_map_and_release_digest():
    from datetime import date
    from bondtrader.data.fundamentals import debt_map, digest_release, release_links
    from bondtrader.data.ratings_web import parse_table_with_header
    from bondtrader.models import Bond, Quote
    settle = date(2026, 9, 11)
    b1 = Bond("A1", name="Аэрфью2Р05", full_name="Аэрофьюэлз 002Р-05", maturity=date(2027, 8, 20), coupon_percent=19.75, issue_size=1_000_000, face_value=1000.0)
    b2 = Bond("A2", name="Аэрфью3Р01", full_name="Аэрофьюэлз003Р-01", maturity=date(2029, 3, 1), offer_date=date(2028, 3, 1), coupon_percent=20.0,
              issue_size=1_400_000, face_value=1000.0)
    dm = debt_map("АЭРОФЬЮЭЛЗ", [(b1, Quote("A1", settle, price=99.0)), (b2, Quote("A2", settle, price=98.0))], {"A1": 19.5}, {"A1"})
    assert round(dm.total_mln) == 2400 and dm.schedule(settle) == {2027: 1000.0, 2028: 1400.0} and dm.schedule(settle, by_offer=False) == {2027: 1000.0, 2029: 1400.0}
    assert "2 выпусках" in dm.describe(settle) and dm.lines[0].in_screen and not dm.lines[1].in_screen
    page = '<div><a href="/releases/2026/sep10a">Подтверждён рейтинг</a><a href="/releases/2025/mar01b">Старый</a><a href="/releases/2026/sep10a">дубль</a></div>'
    assert release_links(page) == ["https://raexpert.ru/releases/2026/sep10a", "https://raexpert.ru/releases/2025/mar01b"]
    rel = """<html><title>Эксперт РА подтвердил рейтинг АО «Аэрофьюэлз» на уровне ruA</title><body><p>10.09.2026.</p>
    <p>Отношение чистого долга к EBITDA на 30.06.2026 составило 2,1x, покрытие процентных платежей EBITDA — 2,8x. Погода хорошая.
    Выручка за 2025 год выросла на 15% до 62 млрд руб. Прогноз по рейтингу стабильный.</p></body></html>"""
    d = digest_release("https://raexpert.ru/releases/2026/sep10a", rel)
    assert "ruA" in d.title and d.date == date(2026, 9, 10) and len(d.sentences) == 2 and "2,1x" in d.sentences[0] and "62 млрд" in d.sentences[1]
    # ссылка на страницу компании из реестра
    tbl = """<table><thead><tr><th>Рейтингуемое лицо</th><th>Рейтинг</th><th>Дата</th></tr></thead><tbody>
    <tr><td><a href="/database/companies/aerofuels/">АО «Аэрофьюэлз»</a></td><td>ruA</td><td>10.09.2026</td></tr></tbody></table>"""
    rs = parse_table_with_header(tbl, "Эксперт РА", "issuer")
    assert rs[0].url == "https://raexpert.ru/database/companies/aerofuels/" and rs[0].rating == "A"


def test_girbo_search_strips_highlight_tags():
    from bondtrader.data.girbo import _clean_org
    org = _clean_org({"inn": "78<strong>0401</strong>6807", "shortName": "АО <strong>АБЗ-1</strong>", "id": 5})
    assert org == {"inn": "7804016807", "shortName": "АО АБЗ-1", "id": 5}


def test_girbo_find_org_validates_candidates():
    from bondtrader.data.girbo import GirboClient, _opf_compatible

    class Fake(GirboClient):
        def __init__(self, items):
            super().__init__()
            self._items = items

        def search(self, query):
            return self._items

    items = [{"inn": "0257013900", "shortName": 'ООО "КЭМП02"'}, {"inn": "7708186108", "shortName": 'АО "ПОЛИПЛАСТ"'}]
    org = Fake(items).find_org("ПОЛИПЛАСТ", expect="Полипласт АО П02-БО-14")
    assert org and org["inn"] == "7708186108"
    # ООО с тем же именем — другое юрлицо, а профсоюз — не эмитент
    items = [{"inn": "2309127380", "shortName": 'ООО "ГИДРОМАШСЕРВИС"'}, {"inn": "1650030847", "shortName": 'ФБСП ПАО "КАМАЗ"'}]
    assert Fake(items).find_org("ГИДРОМАШСЕРВИС", expect="ГИДРОМАШСЕРВИС АО БО-03") is None
    assert Fake(items).find_org("КАМАЗ", expect="КАМАЗ ПАО БО-П15") is None
    items = [{"inn": "7733015025", "shortName": 'АО "ГИДРОМАШСЕРВИС"'}]
    assert Fake(items).find_org("ГИДРОМАШСЕРВИС", expect="ГИДРОМАШСЕРВИС АО БО-03")["inn"] == "7733015025"
    # серия в запросе не должна вести к чужой организации
    assert Fake([{"inn": "6500002920", "shortName": 'ООО "СП15"'}]).find_org("ГК САМОЛЕТ", expect="ГК Самолет БО-П15") is None
    assert _opf_compatible('ПАО "СЕЛИГДАР"', "Селигдар АО 001Р-04") and not _opf_compatible('ООО "СИНАРА"', "СТМ АО 1P4")
    assert _opf_compatible('ООО "АЭРОФЬЮЭЛЗ ГРУПП"', "Аэрофьюэлз 002Р-06")
    # ИНН — как раньше, точное совпадение
    assert Fake([{"inn": "7804016807", "shortName": 'АО "АБЗ-1"'}]).find_org("7804016807")["inn"] == "7804016807"


def test_moex_emitter_info_and_issuer_map_replace():
    """ИНН эмитента из MOEX ISS (emitent_inn по ISIN) и замена ошибочной записи карты ИНН (тёзка по имени → точный ИНН)."""
    from bondtrader.data.financials import IssuerMap, IssuerRecord
    from bondtrader.data.moex import MoexClient
    from bondtrader.models import Bond

    class Fake(MoexClient):
        def __init__(self):
            super().__init__(cache=None)
        def fetch_json(self, path, params=None, ttl=300, retries=5):
            assert path == "/iss/securities.json" and params["q"] == "RU000A10G122"
            return {"securities": {"columns": ["secid", "shortname", "isin", "emitent_id", "emitent_title", "emitent_inn", "emitent_okpo"],
                                   "data": [["RU000A10G122", "ПолипП2Б17", "RU000A10G122", 3592, 'Акционерное общество "Полипласт"', "7708186108", "58042865"]]}}
    info = Fake().emitter_info("RU000A10G122")
    assert info["emitent_inn"] == "7708186108" and info["emitent_id"] == 3592
    b = Bond("RU000A10G122", name="ПолипП2Б17", full_name="Полипласт АО П02-БО-17", isin="RU000A10G122")
    m = IssuerMap([IssuerRecord("7718814016", 'АО "ГК ПОЛИПЛАСТ"', alias="ПолипП2Б17")])
    assert m.lookup(b).inn == "7718814016"
    removed = m.replace_for(b, IssuerRecord("7708186108", 'АО "Полипласт"', alias="ПолипП", isin="RU000A10G122", emitter_id="3592"))
    assert removed == 1 and m.lookup(b).inn == "7708186108" and len(m) == 1
    assert m.replace_for(b, IssuerRecord("7708186108", "x")) == 0 and len(m) == 1      # уже верно — ничего не меняем
