"""Сценарии: при нулевом сдвиге полная доходность ≈ YTW, рост доходности снижает результат, снижение — повышает;
бумага, гасящаяся внутри горизонта, реинвестируется до конца горизонта."""
from datetime import date, timedelta

import pytest

from bondtrader.analytics.bond_math import compute_metrics
from bondtrader.analytics.scenario import scenario_bond, scenario_portfolio
from bondtrader.models import Bond, Quote

SETTLE = date(2026, 9, 12)


def make_bond(secid, mat, cpct, period=91, offer=None):
    face, cval = 1000.0, round(1000 * cpct / 100 * period / 365, 2)
    coupons, d = [], mat
    while d > SETTLE - timedelta(days=period):
        coupons.append((d, cval)); d -= timedelta(days=period)
    coupons.sort()
    return Bond(secid=secid, name=secid, board="TQCB", face_value=face, initial_face_value=face, coupon_percent=cpct,
                coupon_value=cval, coupon_period=period, maturity=mat, next_coupon=next(c for c, _ in coupons if c > SETTLE),
                coupons=coupons, amortizations=[(mat, face)], has_full_schedule=True, list_level=2, offer_date=offer)


def test_zero_shift_matches_ytw_and_monotonic():
    b = make_bond("LONG", date(2029, 3, 1), 18.0)
    q = Quote("LONG", SETTLE, price=99.0, accrued=5.0)
    m = compute_metrics(b, q, SETTLE)
    r = scenario_bond(b, q, SETTLE, [-300, 0, 300], 1.0, metrics=m)
    assert r is not None and not r.matures_in_horizon
    # без сдвига доход за год равен эффективной доходности (реинвестирование под неё же)
    assert r.total_return[0] == pytest.approx(m.yield_worst, abs=0.15)
    assert r.total_return[-300] > r.total_return[0] > r.total_return[300]
    assert r.instant_pnl[0] == pytest.approx(0.0, abs=1e-9)
    # мгновенная переоценка ≈ −мод.дюрация × сдвиг
    assert r.instant_pnl[300] == pytest.approx(-m.modified_duration * 3, rel=0.15)
    assert r.instant_pnl[-300] > 0


def test_bond_maturing_inside_horizon_is_reinvested():
    b = make_bond("SHORT", date(2027, 3, 1), 16.0)
    q = Quote("SHORT", SETTLE, price=100.0, accrued=2.0)
    r = scenario_bond(b, q, SETTLE, [-300, 0, 300], 1.0)
    assert r.matures_in_horizon
    # после погашения деньги работают под сдвинутую ставку: рост ставки даёт БОЛЬШЕ за год, чем снижение
    assert r.total_return[300] > r.total_return[0] > r.total_return[-300]
    assert r.instant_pnl[300] < 0


def test_portfolio_equal_weights():
    a = scenario_bond(make_bond("A", date(2028, 6, 1), 19.0), Quote("A", SETTLE, price=101.0, accrued=3.0), SETTLE, [0, 300])
    b = scenario_bond(make_bond("B", date(2029, 6, 1), 17.0), Quote("B", SETTLE, price=97.0, accrued=3.0), SETTLE, [0, 300])
    total, instant, ytw, dur = scenario_portfolio([a, b])
    assert total[0] == pytest.approx((a.total_return[0] + b.total_return[0]) / 2)
    assert instant[300] == pytest.approx((a.instant_pnl[300] + b.instant_pnl[300]) / 2)
    assert ytw == pytest.approx((a.ytw + b.ytw) / 2) and dur > 0
