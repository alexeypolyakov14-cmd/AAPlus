"""Метрики из релизов агентств: извлечение чисел, ступень по бенчмаркам, разрыв с рейтингом, книга, вторая ось в стратегии."""
from datetime import date

from bondtrader.analytics.quality import quality_stats
from bondtrader.data.fundamentals import ReleaseDigest
from bondtrader.data.metrics import AgencyMetrics, MetricsBook, extract_metrics, implied_grade, metrics_from_digests, rating_gap

AERO = ["По итогам 2025 года долговая нагрузка в терминах скорректированный чистый долг/EBITDA выросла до 2,5х по сравнению с 1,9х годом ранее.",
        "Коэффициент покрытия процентных расходов показателем EBITDA по итогам отчетного периода снизился до 3,1х по сравнению с 5,1х годом ранее.",
        "Рентабельность по EBITDA по итогам года выросла до 11,3% (в 2024 г. – 10,5%).",
        "В результате роста объема заправок выручка Группы увеличилась на 6% относительно уровня 2024 года, до 73,5 млрд руб."]
GMS = ["На 31.12.2024 (далее – «отчетная дата») отношение скорректированного долга к EBITDA по расчетам агентства составило 1,7х (1,5х годом ранее).",
       "По оценкам агентства на отчетную дату показатель EBITDA / %% составил 9,4х (11,8х годом ранее)."]
NKR = ["Долговая нагрузка (отношение совокупного долга к OIBDA) по итогам 2024 года составило 2,5, и по итогам 2025 года ожидается снижение метрики до 2,4.",
       "Покрытие процентов по кредитам и займам операционной прибылью (OIBDA) за 2024 год составило 1,9, а покрытие процентов свободным денежным потоком (FCF) составило 1,6."]


def test_extract_expert_ra_release():
    m = extract_metrics(AERO, "«Эксперт РА» подтвердил кредитный рейтинг ООО «Аэрофьюэлз Групп» на уровне ruA | Эксперт РА")
    assert m["net_debt_ebitda"] == 2.5 and m["net_debt_ebitda_prev"] == 1.9 and m["coverage"] == 3.1
    assert m["ebitda_margin"] == 11.3 and m["revenue_bln"] == 73.5 and m["period"] == "2025" and m["rating"] == "A"
    m2 = extract_metrics(["Показатель чистого долга к EBITDA по итогам отчетного периода составил 3,5х (2,6х годом ранее)."],
                         "«Эксперт РА» понизил кредитный рейтинг ООО «Биннофарм Групп» до уровня ruA- и снял статус «под наблюдением»")
    assert m2["net_debt_ebitda"] == 3.5 and m2["rating"] == "A-"
    m3 = extract_metrics(GMS, "АО «Эксперт РА» присвоило рейтинг компании АО «Гидромашсервис» на уровне ruA")
    assert m3["debt_ebitda"] == 1.7 and m3["coverage"] == 9.4 and m3["period"] == "31.12.2024"


def test_extract_nkr_style_without_x():
    m = extract_metrics(NKR, "НКР присвоило АО «ПСФ „Балтийский проект“» кредитный рейтинг A-.ru со стабильным прогнозом")
    assert m["debt_ebitda"] == 2.5 and m["coverage"] == 1.9 and m["period"] == "2024" and m["rating"] == "A-"
    assert extract_metrics(["Погода хорошая, продажи 5 млрд руб."], "") == {}


def test_implied_grade_and_gap():
    mk = lambda lev, cov, sector="industrial": AgencyMetrics("k", "s", "a", "u", net_debt_ebitda=lev, coverage=cov, sector=sector)
    assert implied_grade(mk(2.5, 3.1)) == "A-"          # худшая из двух метрик
    assert implied_grade(mk(1.7, 9.4)) == "A+"
    assert implied_grade(mk(3.5, None)) == "BBB+"
    assert implied_grade(mk(2.5, 1.9)) == "BBB-"
    assert implied_grade(mk(None, None)) is None
    assert implied_grade(mk(1.0, 10.0, sector="leasing")) is None   # для финансовых бенчмарки не применяются
    assert rating_gap("A-", "BBB+") == -1 and rating_gap("A", "A+") == 1 and rating_gap("A", "A") == 0
    assert rating_gap(None, "A") is None and rating_gap("A", None) is None


def test_book_roundtrip_and_digests(tmp_path):
    d_old = ReleaseDigest("u1", "«Эксперт РА» присвоил рейтинг ООО «Х» на уровне ruA", date(2025, 9, 18), ["Ликвидность высокая."])
    d_new = ReleaseDigest("u2", "«Эксперт РА» подтвердил рейтинг ООО «Х» на уровне ruA", date(2026, 9, 10), AERO)
    m = metrics_from_digests("Х", "Эксперт РА", "transport", [d_new, d_old])
    assert m is not None and m.url == "u2" and m.net_debt_ebitda == 2.5 and m.rating == "A"
    empty = metrics_from_digests("Y", "Эксперт РА", "", [d_old])
    assert empty is not None and not empty.has_metrics
    book = MetricsBook()
    book.upsert(m); book.upsert(empty)
    # старый релиз не вытесняет свежий
    book.upsert(AgencyMetrics("Х", "старый", "Эксперт РА", "u0", release_date=date(2024, 1, 1), net_debt_ebitda=9.0))
    assert book.lookup("Х").net_debt_ebitda == 2.5
    p = tmp_path / "metrics.csv"
    book.to_csv(str(p))
    back = MetricsBook.from_csv(str(p))
    assert len(back) == 2 and back.lookup("Х").coverage == 3.1 and back.lookup("Х").release_date == date(2026, 9, 10)
    assert back.lookup("Y") is not None and back.lookup("Y").leverage is None and back.lookup("Z") is None


def test_quality_stats_and_rank_quality():
    from bondtrader.data.ratings import Rating
    from bondtrader.models import Bond, BondMetrics, Quote
    from bondtrader.screener import ScreenRow
    from bondtrader.strategies.base import MarketContext
    from bondtrader.strategies.gspread import GSpreadStrategy

    def row(secid, full, rating, spread, dur=1.5):
        b = Bond(secid, name=secid, full_name=full, maturity=date(2028, 1, 1), face_value=1000.0, list_level=2, board="TQCB")
        m = BondMetrics(secid, 100.0, 1000.0, 19.0, None, 19.0, dur, dur / 1.19, 3.0, 0.1, 2.0, 19.0, g_spread=spread)
        return ScreenRow(b, Quote(secid, date(2026, 9, 13), price=100.0), m, rating=Rating(full, "Эксперт РА", rating), sector="industrial")
    good = AgencyMetrics("ГИДРОМАШСЕРВИС", "ГМС", "Эксперт РА", "u", net_debt_ebitda=1.7, coverage=9.4, sector="industrial")
    bad = AgencyMetrics("АБЗ-1", "АБЗ", "НКР", "u", debt_ebitda=2.5, coverage=1.9, sector="industrial")
    book = MetricsBook([good, bad])
    rows = [row("GMS1", "Гидромашсервис АО 002Р-01", "A", 420), row("ABZ1", "АБЗ-1 002P-04", "A-", 550), row("NOM1", "Без метрик 01", "A", 600)]
    for i in range(6):   # пиры той же ступени, чтобы медиана считалась
        rows.append(row(f"P{i}", f"Пир {i} БО-01", "A", 300 + 10 * i))
    qs = quality_stats(rows, book)
    assert qs["GMS1"].implied == "A+" and qs["GMS1"].gap == 1 and "лучше рейтинга" in qs["GMS1"].verdict
    assert qs["ABZ1"].implied == "BBB-" and qs["ABZ1"].gap == -3 and "NOM1" not in qs
    ctx = MarketContext(date(2026, 9, 13), rows, quality_stats=qs)
    st = GSpreadStrategy(top_n=5, per_issuer=1, max_duration=4.0, rank="quality", min_peers=3)
    picks = [r.secid for r in st.ranked(ctx)]
    assert picks == ["GMS1"], picks   # АБЗ-1 отсечён разрывом, «без метрик» не участвует
    assert "метрики: чистый долг/EBITDA 1.7x" in st.reason(rows[0], ctx) and "разрыв +1" in st.reason(rows[0], ctx)
    assert "метрики: нет в книге" in st.reason(rows[2], ctx)
