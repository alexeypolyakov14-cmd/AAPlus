"""Бэктест на синтетической истории: доходности ОФЗ снижаются -> цены растут; купоны и погашение учитываются."""
from datetime import date, timedelta

import pandas as pd
import pytest

from bondtrader.analytics.bond_math import accrued_interest, build_cash_flows, duration_convexity, price_from_ytm
from bondtrader.backtest.data import FrameHistoryProvider
from bondtrader.backtest.engine import BacktestEngine, _is_rebalance_day
from bondtrader.backtest.metrics import performance
from bondtrader.models import Bond
from bondtrader.risk import RiskLimits
from bondtrader.screener import ScreenerConfig
from bondtrader.strategies import make_strategy

START, END = date(2024, 1, 9), date(2024, 12, 27)


def make_bond(secid, name, mat, cpct, ofz=True):
    face = 1000
    period = 182
    cval = round(face * cpct / 100 * period / 365, 2)
    # купоны каждые 182 дня назад от погашения
    coupons = []
    d = mat
    while d > START - timedelta(days=200):
        coupons.append((d, cval))
        d -= timedelta(days=period)
    coupons.sort()
    return Bond(secid=secid, name=name, board="TQOB" if ofz else "TQCB", face_value=face, initial_face_value=face,
                coupon_percent=cpct, coupon_value=cval, coupon_period=period, maturity=mat,
                next_coupon=next(c for c, _ in coupons if c > START), coupons=coupons,
                amortizations=[(mat, float(face))], has_full_schedule=True, list_level=1)


def synth_history(bond: Bond, ytm_path):
    rows = {}
    for d, y in ytm_path:
        if d >= bond.maturity:
            break
        # обновляем next_coupon для расчёта НКД
        bond.next_coupon = next((c for c, _ in bond.coupons if c > d), bond.maturity)
        price = price_from_ytm(bond, d, y)
        flows = build_cash_flows(bond, d)
        dur = duration_convexity(flows, d, y / 100)[0]
        rows[pd.Timestamp(d)] = {"close": round(price, 2), "ytm": y, "duration": dur,
                                 "accrued": round(accrued_interest(bond, d), 2), "face": 1000.0, "value": 50e6}
    return pd.DataFrame.from_dict(rows, orient="index")


@pytest.fixture
def world():
    days = [START + timedelta(days=i) for i in range((END - START).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    n = len(days)
    bonds = [
        make_bond("SU_SHORT", "ОФЗ короткая", date(2024, 7, 10), 8.0),
        make_bond("SU_MID", "ОФЗ средняя", date(2027, 3, 3), 7.0),
        make_bond("SU_LONG", "ОФЗ длинная", date(2033, 5, 18), 7.5),
        make_bond("CORP_A", "Альфа БО-1", date(2026, 9, 15), 12.0, ofz=False),
    ]
    # доходности: снижаются с 16% до 13% в течение года (ЦБ смягчает), корпорат +200 б.п.
    paths = {}
    for b in bonds:
        base = 16.0
        extra = 2.0 if not b.is_ofz else 0.0
        paths[b.secid] = [(d, base - 3.0 * i / n + extra) for i, d in enumerate(days)]
    hist = {b.secid: synth_history(b, paths[b.secid]) for b in bonds}
    index = pd.Series([100 * (1 + 0.0004) ** i for i in range(n)], index=pd.DatetimeIndex(days), name="RGBITR")
    keyrate = [(START - timedelta(days=300), 16.0), (START - timedelta(days=100), 16.0), (date(2024, 2, 16), 16.0),
               (date(2024, 6, 7), 15.0), (date(2024, 9, 13), 14.0), (END, 14.0)]
    return bonds, FrameHistoryProvider(hist, index, keyrate)


def test_rebalance_calendar():
    a, b = pd.Timestamp("2024-01-31"), pd.Timestamp("2024-02-01")
    assert _is_rebalance_day(b, a, "monthly") and not _is_rebalance_day(b, b, "monthly")
    assert _is_rebalance_day(b, None, "quarterly")
    assert _is_rebalance_day(pd.Timestamp("2024-01-08"), pd.Timestamp("2024-01-05"), "weekly")
    with pytest.raises(ValueError):
        _is_rebalance_day(b, a, "hourly")


def test_backtest_ladder_with_coupons_and_maturity(world):
    bonds, provider = world
    eng = BacktestEngine(make_strategy("ladder", {"edges": [1, 3], "per_bucket": 1}), bonds, provider, START, END,
                         initial_cash=1_000_000, commission_bp=5, slippage_bp=5, rebalance="monthly",
                         screener_cfg=ScreenerConfig(min_turnover=0, max_bid_ask_pct=100, max_duration=20),
                         risk_limits=RiskLimits(max_weight_per_bond=1.0, max_portfolio_duration=20))
    res = eng.run()
    assert len(res.nav) > 200
    assert res.nav.iloc[0] == pytest.approx(1_000_000, rel=1e-6)
    # доходности падали -> NAV должен вырасти (купон + переоценка)
    assert res.nav.iloc[-1] > 1_050_000
    assert res.coupons_received > 0
    assert res.commissions_paid > 0
    assert res.trades and res.trades[0].side == "BUY"
    # короткая бумага погасилась в июле -> в финальном портфеле её нет, а деньги вернулись
    assert "SU_SHORT" not in res.final_portfolio.positions
    assert any(t.secid == "SU_SHORT" and t.side == "BUY" for t in res.trades)
    s = res.stats
    assert s.total_return > 5 and s.max_drawdown <= 0 and s.volatility > 0
    assert s.benchmark_total_return is not None and s.beta is not None
    assert res.regimes and set(res.regimes.values()) <= {"easing", "hold", "tightening"}
    summ = res.summary()
    assert summ["trades"] == len(res.trades)


def test_backtest_rate_cycle_extends_duration_on_easing(world):
    bonds, provider = world
    st = make_strategy("rate_cycle", {"top_n": 1, "long_dur": 5.0, "band": 3.0})
    eng = BacktestEngine(st, bonds, provider, START, END, rebalance="monthly",
                         screener_cfg=ScreenerConfig(min_turnover=0, max_bid_ask_pct=100, max_duration=20),
                         risk_limits=RiskLimits(max_weight_per_bond=1.0, max_portfolio_duration=20))
    res = eng.run()
    # после снижения ставки в июне стратегия должна держать длинную ОФЗ
    late = [w for d, w in res.weights_history.items() if d >= date(2024, 7, 1)]
    assert late and any("SU_LONG" in w for w in late)
    assert res.nav.iloc[-1] > res.nav.iloc[0]


def test_performance_metrics_basic():
    idx = pd.date_range("2024-01-01", periods=260, freq="B")
    nav = pd.Series([100 * (1.0004 ** i) for i in range(260)], index=idx)
    s = performance(nav, rf_annual_pct=0.0)
    assert s.total_return == pytest.approx((1.0004 ** 259 - 1) * 100, rel=1e-6)
    assert s.max_drawdown == 0 and s.sharpe > 0
    nav2 = nav.copy()
    nav2.iloc[100:110] *= 0.9
    assert performance(nav2).max_drawdown < -5
    with pytest.raises(ValueError):
        performance(nav.iloc[:1])
