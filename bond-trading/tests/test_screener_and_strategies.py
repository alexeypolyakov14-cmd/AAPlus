import json
import os
from datetime import date

import pytest

from bondtrader.analytics.curve import ZeroCurve
from bondtrader.data.cbr import KeyRateView
from bondtrader.data.moex import parse_board_securities
from bondtrader.portfolio import Fill, Portfolio
from bondtrader.risk import RiskLimits, RiskManager, orders_from_targets
from bondtrader.screener import Screener, ScreenerConfig, build_curve, to_dataframe
from bondtrader.strategies import MarketContext, Side, make_strategy

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
SETTLE = date(2025, 6, 2)


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def universe():
    return parse_board_securities(load("bonds_board.json"), SETTLE)


@pytest.fixture
def curve():
    return ZeroCurve.from_moex_zcyc(load("zcyc.json"))


@pytest.fixture
def rows(universe, curve):
    return Screener(ScreenerConfig(min_turnover=1e6, max_list_level=2, max_bid_ask_pct=1.0)).run(universe, curve, SETTLE)


def test_screener_filters(universe, curve):
    s = Screener(ScreenerConfig(min_turnover=1e6, max_list_level=2))
    rows = s.run(universe, curve, SETTLE)
    ids = {r.secid for r in rows}
    assert "SU29014RMFS6" not in ids and s.rejected["SU29014RMFS6"] == "флоатер"
    assert "SU52002RMFS1" not in ids and s.rejected["SU52002RMFS1"] == "линкер"
    assert "RU000A1090Y7" not in ids and s.rejected["RU000A1090Y7"].startswith("листинг")
    assert "XS0000000001" not in ids
    assert "RU000A100XY9" not in ids and s.rejected["RU000A100XY9"].startswith("валюта")
    assert "RU000A100AMR" not in ids and s.rejected["RU000A100AMR"] == "широкий bid/ask"
    assert "RU000A105XX1" not in ids  # спред 2500 б.п. -> дистресс
    assert "SU26234RMFS3" not in ids  # дюрация < 0.2
    assert "SU26238RMFS4" in ids and "RU000A104ZK2" in ids
    assert rows == sorted(rows, key=lambda r: r.score, reverse=True)
    df = to_dataframe(rows)
    assert {"secid", "ytm", "duration", "g_spread", "score"} <= set(df.columns)
    mts = next(r for r in rows if r.secid == "RU000A104ZK2")
    assert "оферта" in mts.flags and mts.metrics.ytm_to_offer is not None
    # у корпоратов положительный G-спред, у ОФЗ около нуля
    ofz = [r for r in rows if r.bond.is_ofz]
    corp = [r for r in rows if not r.bond.is_ofz]
    assert all(abs(r.metrics.g_spread) < 60 for r in ofz)
    assert all(r.metrics.g_spread > 0 for r in corp)


def test_build_curve_fallback(universe):
    c = build_curve(universe, SETTLE, zcyc_payload=None)
    assert c.source == "ofz_fit" and len(c) >= 3
    assert c.yield_at(1) > c.yield_at(10)   # инверсия, как в фикстуре
    c2 = build_curve(universe, SETTLE, zcyc_payload=load("zcyc.json"))
    assert c2.source == "moex_zcyc"


def test_ladder_strategy(rows, curve):
    st = make_strategy("ladder", {"edges": [1, 2, 3, 5], "per_bucket": 1})
    ctx = MarketContext(SETTLE, rows, curve)
    w = st.targets(ctx)
    assert w and abs(sum(w.values()) - 1.0) < 1e-9
    durs = sorted(ctx.by_id[s].metrics.macaulay_duration for s in w)
    assert len(w) == 5 and durs[0] < 1 and durs[-1] >= 5
    sig = st.generate(ctx)
    assert all(s.side == Side.BUY for s in sig)
    # удержание: купленные бумаги остаются
    pf = Portfolio(cash=0)
    first = next(iter(w))
    pf.apply_fill(Fill(first, "BUY", 10, 90, 1, 1000))
    ctx2 = MarketContext(SETTLE, rows, curve, portfolio=pf)
    assert first in st.targets(ctx2)
    assert any(s.side == Side.HOLD and s.secid == first for s in st.generate(ctx2))


def test_spread_strategy_cross_section_and_history(rows, curve):
    st = make_strategy("spread", {"entry_z": 0.5, "exit_z": -0.5, "top_n": 3, "ofz_anchor": 0.3})
    ctx = MarketContext(SETTLE, rows, curve)
    z = st.zscores(ctx)
    assert z and all(not ctx.by_id[s].bond.is_ofz for s in z)
    w = st.targets(ctx)
    ofz_w = sum(v for s, v in w.items() if ctx.by_id[s].bond.is_ofz)
    assert ofz_w == pytest.approx(0.3)
    # временная z-оценка: история узких спредов делает текущий спред «широким»
    wide = "RU000A103WV8"
    ctx_h = MarketContext(SETTLE, rows, curve, spread_history={wide: [100.0] * 50 + [120.0] * 10})
    z2 = st.zscores(ctx_h)
    assert z2[wide] > 3
    # выход: держим бумагу, чей спред «сжался» (история широких спредов)
    pf = Portfolio(cash=0)
    pf.apply_fill(Fill(wide, "BUY", 5, 88, 14, 1000))
    ctx_x = MarketContext(SETTLE, rows, curve, portfolio=pf, spread_history={wide: [1900.0, 2100.0] * 30})
    sig = st.generate(ctx_x)
    assert any(s.secid == wide and s.side == Side.SELL for s in sig)


def test_rate_cycle_strategy(rows, curve):
    st = make_strategy("rate_cycle", {"top_n": 2})
    easing = KeyRateView(20.0, date(2025, 6, 6), -1.0, 1, "easing", 21, 16)
    tight = KeyRateView(21.0, date(2025, 5, 1), 2.0, 3, "tightening", 21, 16)
    hold = KeyRateView(21.0, date(2024, 10, 28), 2.0, 3, "hold", 21, 16)
    d_e = st.target_duration(MarketContext(SETTLE, rows, curve, easing))[0]
    d_t = st.target_duration(MarketContext(SETTLE, rows, curve, tight))[0]
    d_h = st.target_duration(MarketContext(SETTLE, rows, curve, hold))[0]
    assert d_t < d_h < d_e
    assert d_h == st.mid_long_dur  # кривая в фикстуре инвертирована
    w = st.targets(MarketContext(SETTLE, rows, curve, easing))
    assert w and all(rows_by := MarketContext(SETTLE, rows, curve).by_id[s].bond.is_ofz for s in w)
    avg_dur = sum(w[s] * MarketContext(SETTLE, rows, curve).by_id[s].metrics.macaulay_duration for s in w)
    w_t = st.targets(MarketContext(SETTLE, rows, curve, tight))
    avg_dur_t = sum(w_t[s] * MarketContext(SETTLE, rows, curve).by_id[s].metrics.macaulay_duration for s in w_t)
    assert avg_dur > avg_dur_t
    # без данных по ставке — режим hold, стратегия всё равно работает
    assert st.targets(MarketContext(SETTLE, rows, curve, None))


def test_carry_strategy_and_ofz_floor(rows, curve):
    st = make_strategy("carry", {"top_n": 4, "max_duration": 5, "ofz_min_share": 0.4})
    ctx = MarketContext(SETTLE, rows, curve)
    er = st.expected_returns(ctx)
    assert all(ctx.by_id[s].metrics.macaulay_duration <= 5 for s in er)
    w = st.targets(ctx)
    assert abs(sum(w.values()) - 1.0) < 1e-9 and len(w) == 4
    ofz_share = sum(v for s, v in w.items() if ctx.by_id[s].bond.is_ofz)
    assert ofz_share >= 0.4 - 1e-9
    assert all("E[R]" in r for r in st.explain(ctx).values())


def test_risk_enforce_and_orders(rows):
    by_id = {r.secid: r for r in rows}
    rm = RiskManager(RiskLimits(max_weight_per_bond=0.25, max_weight_per_issuer=0.3, max_corporate_share=0.5))
    corp = [s for s in by_id if not by_id[s].bond.is_ofz][:3]
    targets = {corp[0]: 0.5, corp[1]: 0.3, corp[2]: 0.2}
    w, notes = rm.enforce_targets(targets, by_id)
    assert max(w.values()) <= 0.25 + 1e-9
    assert sum(w.values()) <= 0.5 + 1e-9
    assert any(n.code == "bond_cap" for n in notes) and any(n.code == "corp_share" for n in notes)
    # ордера из целей
    pf = Portfolio(cash=1_000_000)
    orders = orders_from_targets(pf, w, by_id, strategy="test")
    assert orders and all(o.side == "BUY" for o in orders)
    spent = sum(o.qty * by_id[o.secid].metrics.dirty_price for o in orders)
    assert spent <= 1_000_000 * 0.5 + 1e-6
    assert not [v for v in rm.check_orders(orders, by_id, pf, 1_000_000) if v.hard]
    # исполняем и проверяем риск-метрики
    for o in orders:
        r = by_id[o.secid]
        pf.apply_fill(Fill(o.secid, o.side, o.qty, o.price, r.quote.accrued, r.bond.face_value, commission=10))
    risk = rm.portfolio_risk(pf, by_id)
    assert risk.nav == pytest.approx(1_000_000 - 10 * len(orders), rel=1e-6)
    assert risk.dv01 > 0 and risk.var_1d_95 > 0 and 0 < risk.corporate_share <= 0.5 + 1e-6
    # продажа всего -> SELL ордера первыми и ошибка при коротких продажах
    sells = orders_from_targets(pf, {}, by_id)
    assert sells and all(o.side == "SELL" for o in sells)
    from bondtrader.portfolio import Order
    bad = rm.check_orders([Order("SU26238RMFS4", "SELL", 5)], by_id, Portfolio(cash=0), 1)
    assert any(v.code == "short" for v in bad)


def test_portfolio_accounting():
    pf = Portfolio(cash=100_000)
    pf.apply_fill(Fill("A", "BUY", 10, 95.0, 5.0, 1000, commission=5))
    assert pf.cash == pytest.approx(100_000 - 10 * 955 - 5)
    pf.apply_fill(Fill("A", "BUY", 10, 97.0, 5.0, 1000))
    assert pf.positions["A"].avg_price == pytest.approx(96.0)
    pf.apply_coupon("A", 40)
    assert pf.coupons_received == 800
    pf.apply_fill(Fill("A", "SELL", 5, 98.0, 1.0, 1000))
    assert pf.realized_pnl == pytest.approx(5 * 1000 * 0.02)
    pf.apply_redemption("A", 1000, full=True)
    assert "A" not in pf.positions
    assert pf.nav({}) == pytest.approx(pf.cash)
    d = pf.to_dict()
    assert Portfolio.from_dict(d).cash == pf.cash


def test_gspread_strategy_ranks_by_spread():
    from bondtrader.cli import build_parser
    from bondtrader.config import Settings
    from bondtrader.cli import _build_context
    from bondtrader.strategies import MarketContext, make_strategy
    import os
    fix = os.path.join(os.path.dirname(__file__), "fixtures")
    args = build_parser().parse_args(["--fixtures", fix, "signals", "-s", "gspread"])
    snap, rows, pf, _ = _build_context(args, Settings.load(None))
    st = make_strategy("gspread", {"top_n": 3, "per_issuer": 1, "ofz_min_share": 0.1})
    ctx = MarketContext(snap.settle, rows, snap.curve, snap.keyrate, pf)
    w = st.targets(ctx)
    corp = [s for s in w if not ctx.by_id[s].bond.is_ofz]
    assert 1 <= len(corp) <= 3 and abs(sum(w.values()) - 1.0) < 1e-9
    spreads = [ctx.by_id[s].metrics.g_spread for s in corp]
    assert spreads == sorted(spreads, reverse=True)
    best = max((r.metrics.g_spread for r in rows if not r.bond.is_ofz and not r.bond.is_floater and r.metrics.g_spread is not None), default=None)
    assert best is None or spreads[0] == best
    assert all("G-спред" in st.explain(ctx)[s] for s in corp)


def test_issuer_key_merges_series_suffixes():
    from bondtrader.models import issuer_key_of
    assert issuer_key_of("БалтЛизП16") == issuer_key_of("БалтЛизП15") == "БАЛТЛИЗ"
    assert issuer_key_of("АРЛФ1Р02") == issuer_key_of("АРЛФ1Р01") == "АРЛФ"
    assert issuer_key_of("iКарРус1P6") == "IКАРРУС" and issuer_key_of("СЕРГВ БО-2") == "СЕРГВ"
    assert issuer_key_of("NSKATD-03") == issuer_key_of("NSKATD1Р01")
    assert issuer_key_of("АПРИ 2Р13") == "АПРИ" and issuer_key_of("ВИС Ф БП04") == "ВИС Ф"


def test_issuer_key_prefers_full_name_without_legal_forms():
    from bondtrader.models import Bond
    a = Bond("A", name="Сегежа3P6R", full_name="Сегежа Групп 003P-06R")
    b = Bond("B", name="Сегеж3P10R", full_name="Сегежа Групп ПАО 003P-10R")
    assert a.issuer_key == b.issuer_key == "СЕГЕЖА ГРУПП"
    assert Bond("C", name="Роделен2P3", full_name="ЛК Роделен БО 002P-03").issuer_key == "РОДЕЛЕН"


def test_gspread_peers_ranking():
    from bondtrader.data.ratings import Rating
    from bondtrader.models import Bond, BondMetrics, Quote
    from bondtrader.screener import ScreenRow
    from bondtrader.strategies import MarketContext, make_strategy
    from bondtrader.portfolio import Portfolio
    from datetime import date

    def row(secid, spread, rating):
        b = Bond(secid, name=secid)
        q = Quote(secid, date(2025, 6, 2), price=100.0, turnover=5e6)
        m = BondMetrics(secid, 100.0, 1000.0, 20.0, None, 20.0, 1.5, 1.3, 0, 0.1, 1.5, 15, spread)
        return ScreenRow(b, q, m, rating=Rating("x", "y", rating) if rating else None)
    rows = [row("A1", 500, "A"), row("A2", 600, "A"), row("A3", 900, "A"),        # A3 платит +300 к медиане A
            row("B1", 1500, "BB"), row("B2", 1550, "BB"), row("B3", 1400, "BB")]   # BB1..3 около медианы
    ctx = MarketContext(date(2025, 6, 2), rows, None, None, Portfolio(cash=1e6))
    st = make_strategy("gspread", {"top_n": 2, "rank": "peers", "min_excess_bp": 0, "min_peers": 2, "per_issuer": 0, "dur_window": 0})
    w = st.targets(ctx)
    assert list(w) == ["A3", "B2"]                      # A3 (+350 к медиане A1/A2) выше B2 (+100), хотя сырой спред B2 втрое больше
    assert "к медиане пиров 550 (A, n=2)" in st.explain(ctx)["A3"] and "дороже 100%" in st.explain(ctx)["A3"]
    st2 = make_strategy("gspread", {"top_n": 5, "rank": "peers", "min_excess_bp": 150, "min_peers": 2, "per_issuer": 0, "dur_window": 0})
    assert list(st2.targets(ctx)) == ["A3"]             # только те, кто платит больше соседей хотя бы на 150 б.п.


def test_peer_group_widening():
    from datetime import date
    from bondtrader.analytics.peers import peer_stats
    from bondtrader.data.ratings import Rating
    from bondtrader.models import Bond, BondMetrics, Quote
    from bondtrader.screener import ScreenRow

    def row(secid, spread, rating, dur=1.5, sector="leasing"):
        b = Bond(secid, name=secid)
        q = Quote(secid, date(2025, 6, 2), price=100.0, turnover=5e6)
        m = BondMetrics(secid, 100.0, 1000.0, 20.0, None, 20.0, dur, dur * 0.9, 0, 0.1, 1.5, 15, spread)
        return ScreenRow(b, q, m, rating=Rating("x", "y", rating) if rating else None, sector=sector)
    uni = [row("X", 1500, "BB-"), row("P1", 1000, "BB-"), row("P2", 1100, "BB-", sector="mfo"), row("Q1", 900, "BB"), row("Q2", 950, "BB+"),
           row("F1", 800, "BB-", dur=4.0), row("U1", 700, None), row("U2", 750, None)]
    ps = peer_stats(uni[0], uni, min_peers=2, same_sector=True, dur_window=1.0)
    assert ps.n == 1 or ps.group.startswith("BB-")      # с сектором пиров мало (только P1) → расширение
    ps = peer_stats(uni[0], uni, min_peers=2, same_sector=False, dur_window=1.0)
    assert ps.n == 2 and ps.median == 1050 and ps.excess == 450 and ps.pct_rank == 1.0 and ps.widened == 0 and ps.group == "BB-, дюрация 0.5–2.5"
    ps = peer_stats(uni[0], uni, min_peers=4, dur_window=1.0)
    assert ps.n == 4 and ps.group.startswith("BB+…B+") and ps.widened == 2   # ±1 ступень: P1, P2, Q1, Q2
    ps = peer_stats(uni[6], uni, min_peers=1)
    assert ps.n == 1 and ps.group == "без рейтинга, дюрация 0.5–2.5" and ps.excess == -50
