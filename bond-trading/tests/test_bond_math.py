from datetime import date

import pytest

from bondtrader.models import Bond, Quote
from bondtrader.analytics.bond_math import (
    accrued_interest, build_cash_flows, compute_metrics, dirty_price_from_yield,
    duration_convexity, price_change_for_yield_shift, price_from_ytm, ytm_from_dirty_price,
)
from bondtrader.analytics.curve import ZeroCurve

SETTLE = date(2025, 6, 2)


def ofz_pd():
    # Условная ОФЗ-ПД: купон 7% (34.9 руб. раз в 182 дня), погашение через ~3 года
    return Bond(
        secid="SU26TEST", name="ОФЗ 26TEST", face_value=1000, initial_face_value=1000,
        coupon_percent=7.0, coupon_value=34.9, coupon_period=182,
        next_coupon=date(2025, 9, 10), maturity=date(2028, 6, 7),
    )


def test_cash_flows_synthetic_schedule():
    b = ofz_pd()
    flows = build_cash_flows(b, SETTLE)
    assert flows[0].date == date(2025, 9, 10)
    assert flows[-1].date == b.maturity
    assert flows[-1].principal == 1000
    # 6 полных купонов + возможный частичный купон в дату погашения
    assert sum(1 for f in flows if f.coupon > 0) >= 6
    assert all(f.date > SETTLE for f in flows)


def test_price_yield_roundtrip():
    b = ofz_pd()
    flows = build_cash_flows(b, SETTLE)
    for y in (0.05, 0.12, 0.20):
        dirty = dirty_price_from_yield(flows, SETTLE, y)
        assert abs(ytm_from_dirty_price(flows, SETTLE, dirty) - y) < 1e-8


def test_par_bond_ytm_close_to_coupon():
    # Купон 7% при цене 100 (сразу после купона) -> эффективная доходность ~7.1% (полугодовое реинвестирование)
    b = Bond(secid="X", face_value=1000, coupon_value=35, coupon_period=182,
             next_coupon=date(2025, 12, 1), maturity=date(2030, 6, 1))
    settle = date(2025, 6, 2)
    flows = build_cash_flows(b, settle)
    dirty = 1000 + accrued_interest(b, settle)
    y = ytm_from_dirty_price(flows, settle, dirty) * 100
    assert 6.9 < y < 7.4


def test_accrued_interest_linear():
    b = ofz_pd()
    # до купона 100 дней из 182 -> прошло 82 дня
    settle = date(2025, 9, 10) - __import__("datetime").timedelta(days=100)
    ai = accrued_interest(b, settle)
    assert abs(ai - 34.9 * 82 / 182) < 1e-9
    assert accrued_interest(b, date(2025, 9, 10)) == pytest.approx(34.9)


def test_duration_and_dv01_sanity():
    b = ofz_pd()
    q = Quote(secid=b.secid, trade_date=SETTLE, price=85.0, accrued=accrued_interest(b, SETTLE))
    m = compute_metrics(b, q)
    assert m is not None
    assert 2.0 < m.macaulay_duration < 3.1
    assert m.modified_duration < m.macaulay_duration
    assert m.ytm > 7  # дисконт -> доходность выше купона
    assert m.dv01 > 0
    # проверка дюрации численно: цена при сдвиге +100 б.п.
    p_up = price_from_ytm(b, SETTLE, m.ytm + 1.0)
    approx = price_change_for_yield_shift(m, 100)
    actual = ((b.face_value * p_up / 100 + q.accrued) / m.dirty_price - 1) * 100
    assert abs(approx - actual) < 0.05


def test_yield_to_offer_uses_put_date():
    b = Bond(secid="CORP1", name="Корп 1", face_value=1000, coupon_value=50, coupon_period=182,
             next_coupon=date(2025, 9, 1), maturity=date(2030, 9, 1),
             offer_date=date(2026, 9, 1), buyback_price=100)
    q = Quote(secid="CORP1", trade_date=SETTLE, price=97.0, accrued=accrued_interest(b, SETTLE))
    m = compute_metrics(b, q)
    assert m.ytm_to_offer is not None
    # дисконт «отыгрывается» быстрее к оферте -> доходность к оферте выше
    assert m.ytm_to_offer > m.ytm
    assert m.yield_worst == m.ytm


def test_amortization_schedule_from_bondization():
    b = Bond(secid="AMORT", face_value=1000, initial_face_value=1000, coupon_value=40, coupon_period=182,
             next_coupon=date(2025, 12, 1), maturity=date(2026, 12, 1),
             coupons=[(date(2025, 12, 1), 40.0), (date(2026, 6, 1), 40.0), (date(2026, 12, 1), 20.0)],
             amortizations=[(date(2026, 6, 1), 500.0), (date(2026, 12, 1), 500.0)],
             has_full_schedule=True)
    flows = build_cash_flows(b, SETTLE)
    assert sum(f.principal for f in flows) == pytest.approx(1000)
    assert [f.principal for f in flows] == [0.0, 500.0, 500.0]
    assert b.has_amortization


def test_floater_detection():
    assert Bond(secid="SU29014RMFS6", name="ОФЗ 29014").is_floater
    assert not Bond(secid="SU26238RMFS4", name="ОФЗ 26238").is_floater
    assert Bond(secid="RU000A1", name="Газпнф3P8R ПК").is_floater


def test_metrics_returns_none_for_matured_or_no_price():
    b = ofz_pd()
    assert compute_metrics(b, Quote("x", SETTLE, price=None)) is None
    assert compute_metrics(b, Quote("x", date(2031, 1, 1), price=100)) is None


def test_g_spread_from_curve():
    curve = ZeroCurve(SETTLE, [(0.5, 18.0), (1, 17.0), (3, 15.0), (10, 14.0)])
    b = ofz_pd()
    q = Quote(secid=b.secid, trade_date=SETTLE, price=80.0, accrued=accrued_interest(b, SETTLE))
    m = compute_metrics(b, q, curve=curve)
    assert m.g_spread is not None
    expected = (m.ytm - curve.yield_at(m.macaulay_duration)) * 100
    assert m.g_spread == pytest.approx(expected)


def test_coupon_payments_between_synthetic_and_full():
    from bondtrader.analytics.bond_math import coupon_payments_between
    b = ofz_pd()  # next_coupon 2025-09-10, период 182
    # исторический период: синтез назад от next_coupon
    pays = coupon_payments_between(b, date(2024, 1, 1), date(2025, 6, 2))
    assert [d for d, _ in pays] == [date(2024, 3, 13), date(2024, 9, 11), date(2025, 3, 12)]
    assert all(v == 34.9 for _, v in pays)
    # будущий период, не дальше погашения
    fut = coupon_payments_between(b, date(2028, 1, 1), date(2030, 1, 1))
    assert fut and fut[-1][0] <= b.maturity
    # полный график: неизвестный купон = последнему известному
    b.coupons = [(date(2025, 9, 10), 34.9), (date(2026, 3, 11), None)]
    b.has_full_schedule = True
    assert coupon_payments_between(b, date(2025, 6, 2), date(2026, 12, 31)) == [(date(2025, 9, 10), 34.9), (date(2026, 3, 11), 34.9)]


def test_puttable_bond_metrics_to_offer_when_coupons_unknown():
    import json, os
    from bondtrader.data.moex import apply_bondization, parse_bondization
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "bondization_mts.json"), encoding="utf-8") as f:
        sched = parse_bondization(json.load(f))
    b = Bond(secid="RU000A104ZK2", name="МТС 1P-21", face_value=1000, coupon_value=49.86, coupon_period=182,
             next_coupon=date(2025, 9, 1), maturity=date(2029, 3, 1))
    apply_bondization(b, sched, today=SETTLE)
    curve = ZeroCurve(SETTLE, [(0.5, 18.0), (1, 17.0), (3, 15.0), (10, 14.0)])
    q = Quote(secid=b.secid, trade_date=SETTLE, price=101.0, accrued=accrued_interest(b, SETTLE))
    m = compute_metrics(b, q, curve=curve)
    # купоны после оферты неизвестны -> считаем к оферте 2026-09-01: дюрация ~1.2 года
    assert 1.0 < m.macaulay_duration < 1.3
    assert m.yield_worst == m.ytm_to_offer
    assert m.g_spread == pytest.approx((m.yield_worst - curve.yield_at(m.macaulay_duration)) * 100)
