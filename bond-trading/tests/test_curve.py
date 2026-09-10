from datetime import date

import pytest

from bondtrader.analytics.curve import ZeroCurve


def test_interpolation_and_extrapolation():
    c = ZeroCurve(date(2025, 1, 10), [(1, 10.0), (2, 12.0), (5, 15.0)])
    assert c.yield_at(1) == 10.0
    assert c.yield_at(1.5) == pytest.approx(11.0)
    assert c.yield_at(0.1) == 10.0
    assert c.yield_at(30) == 15.0
    assert c.slope(1, 5) == pytest.approx(5.0)
    assert c.shifted(50).yield_at(2) == pytest.approx(12.5)


def test_from_moex_zcyc_payload():
    payload = {"yearyields": {
        "columns": ["tradedate", "tradetime", "period", "value"],
        "data": [["2025-06-02", "18:45:00", 0.25, 19.5], ["2025-06-02", "18:45:00", 1.0, 18.2],
                 ["2025-06-02", "18:45:00", 5.0, 15.9], ["2025-06-02", "18:45:00", 10.0, 15.1]],
    }}
    c = ZeroCurve.from_moex_zcyc(payload)
    assert c.trade_date == date(2025, 6, 2)
    assert c.source == "moex_zcyc"
    assert c.yield_at(3) == pytest.approx(18.2 + (15.9 - 18.2) * 2 / 4)
    assert c.slope() < 0  # инверсия


def test_from_ofz_metrics_fallback():
    items = [(0.9, 18.0), (1.1, 18.4), (2.0, 17.0), (4.8, 15.5), (5.2, 15.7), (9.5, 15.0)]
    c = ZeroCurve.from_ofz_metrics(items)
    assert c.source == "ofz_fit"
    assert c.yield_at(1) == pytest.approx(18.2)
    assert c.yield_at(5) == pytest.approx(15.6)
    with pytest.raises(ValueError):
        ZeroCurve.from_ofz_metrics([(1, 10)])
