from bondtrader.data.financials import Statement, compute_metrics, implied_grade


def test_credit_metrics_and_flags():
    good = Statement("1", 2025, {"2110": 10000, "2200": 2000, "2400": 1200, "1300": 5000, "1410": 3000, "1510": 1000,
                                 "1250": 800, "1240": 200, "2330": 400, "1400": 3200, "1500": 2500, "1200": 4000})
    m = compute_metrics(good)
    assert m.total_debt == 4000 and m.net_debt == 3000 and m.interest_coverage == 5.0
    assert m.net_debt_to_ebit == 1.5 and m.short_debt_share == 0.25 and not m.flags and m.score == 100
    assert implied_grade(m.score) == "A"
    bad = Statement("2", 2025, {"2110": 5000, "2200": -100, "2400": -500, "1300": -200, "1410": 500, "1510": 4000,
                                "1250": 100, "2330": 900, "1400": 600, "1500": 6000, "1200": 3000})
    prev = Statement("2", 2024, {"2110": 9000, "1300": 1000})
    mb = compute_metrics(bad, prev)
    assert "отрицательный капитал" in mb.flags and "операционный убыток" in mb.flags
    assert any("рефинансирования" in f for f in mb.flags) and any("выручка" in f for f in mb.flags)
    assert mb.score < 20 and implied_grade(mb.score) == "CCC"
