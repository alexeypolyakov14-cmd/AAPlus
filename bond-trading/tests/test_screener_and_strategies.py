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


def test_issuer_key_keeps_numbers_in_names_and_drops_series():
    """Номер — часть имени (ТГК-14, А101), кавычки/ОПФ/серии — нет: раньше выходили ключи «ПАО», «А», «ТГК», «ИКС»."""
    from bondtrader.models import Bond, issuer_key_of_full
    assert Bond("A", name="ТГК-14 1Р2", full_name='ПАО "ТГК-14" 001Р-02').issuer_key == "ТГК-14"
    assert Bond("B", name="ТГК-14 1Р5", full_name="ТГК-14 001Р-06").issuer_key == "ТГК-14"
    assert issuer_key_of_full("А101 БО-001Р-03") == "А101"
    assert issuer_key_of_full("Банк ВТБ СУБ-Т1-Р1") == "БАНК ВТБ" and issuer_key_of_full("РЖД ОАО ЗО28-1-Р") == "РЖД"
    assert issuer_key_of_full("Аэрофьюэлз-002Р-04") == issuer_key_of_full("Аэрофьюэлз002Р-06") == issuer_key_of_full("Аэрофьюэлз 002Р-05") == "АЭРОФЬЮЭЛЗ"
    assert issuer_key_of_full("О'КЕЙ ООО 001P-06") == "ОКЕЙ" and issuer_key_of_full("Сбер Sb42R") == "СБЕР"
    assert issuer_key_of_full("Трансмашхолдинг АО ПБО-08") == "ТРАНСМАШХОЛДИНГ" and issuer_key_of_full("ГПБ (АО) БО 005Р-02Р") == "ГПБ"
    assert issuer_key_of_full("КАМАЗ БО-П15") == issuer_key_of_full("КАМАЗ БО-П20") == "КАМАЗ"
    assert issuer_key_of_full("ТАЛЬВЕН БО-П01") == "ТАЛЬВЕН" and issuer_key_of_full("Пионер-Лизинг БО-П04") == "ПИОНЕР-ЛИЗИНГ"
    assert issuer_key_of_full("Селигдар GOLD01") == issuer_key_of_full("Селигдар GOLD03") == "СЕЛИГДАР"
    assert issuer_key_of_full("АЛИУМ01Р1") == issuer_key_of_full("АЛИУМ01Р2") == "АЛИУМ" and issuer_key_of_full("АБЗ-1 002Р-06") == "АБЗ-1"
    assert issuer_key_of_full("Т Плюс 001P-01") == "Т ПЛЮС" and issuer_key_of_full("Сегежа3P6R") == "СЕГЕЖА"
    assert issuer_key_of_full("iКаршеринг Руссия 001P-03") == issuer_key_of_full("Каршеринг Руссия 001P-06") == "КАРШЕРИНГ РУССИЯ"
    assert issuer_key_of_full("sГТЛК 2P-12") == "ГТЛК"


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
    assert "пиры: +350 б.п. к медиане 550 (A, n=2)" in st.explain(ctx)["A3"] and "дороже 100%" in st.explain(ctx)["A3"]
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
    assert ps.n == 4 and ps.group == "BB…B+" and ps.widened == 3          # ±1 ступень + без окна дюрации: P1, P2, Q1, F1
    ps = peer_stats(uni[6], uni, min_peers=1)
    assert ps.n == 1 and ps.group == "без рейтинга, сектор leasing, дюрация 0.5–2.5" and ps.excess == -50   # U1 vs U2: свой сектор


def _row(secid, spread, rating, dur=1.5, sector="leasing", issuer=None, ytm_moex=None, dur_moex=None):
    from datetime import date
    from bondtrader.data.ratings import Rating
    from bondtrader.models import Bond, BondMetrics, Quote
    from bondtrader.screener import ScreenRow
    b = Bond(secid, name=secid, full_name=f"{issuer or secid} 001P-01")
    q = Quote(secid, date(2025, 6, 2), price=100.0, turnover=5e6, ytm_moex=ytm_moex, duration_moex=dur_moex)
    m = BondMetrics(secid, 100.0, 1000.0, 20.0, None, 20.0, dur, dur * 0.9, 0, 0.1, 1.5, 15, spread)
    return ScreenRow(b, q, m, rating=Rating("x", "y", rating) if rating else None, sector=sector)


def test_unrated_peers_compare_within_sector_first():
    """Безрейтинговые: сначала свой сектор (лизинг с лизингом), а не вся разношёрстная корзина без рейтинга."""
    from bondtrader.analytics.peers import peer_stats
    x = _row("X", 1500, None, sector="leasing")
    uni = [x, _row("L1", 1000, None), _row("L2", 1100, None), _row("B1", 100, None, sector="bank"), _row("B2", 150, None, sector="bank"),
           _row("R1", 900, "BB", sector="leasing")]
    ps = peer_stats(x, uni, min_peers=2, same_sector=False, dur_window=1.0)
    assert ps.n == 2 and ps.median == 1050 and "сектор leasing" in ps.group and "без рейтинга" in ps.group   # банки и BB не пиры
    lonely = _row("Y", 1500, None, sector="it")
    ps2 = peer_stats(lonely, uni + [lonely], min_peers=2, same_sector=False, dur_window=1.0)
    assert ps2.n == 5 and "все сектора" in ps2.group and ps2.widened == 1                                   # своего сектора нет → все 5 безрейтинговых


def test_issuer_curve_leave_one_out():
    from bondtrader.analytics.issuer_curve import issuer_curves
    iss = [_row("E1", 800, "BBB", dur=0.5, issuer="Эмитент"), _row("E2", 900, "BBB", dur=1.5, issuer="Эмитент"),
           _row("E3", 1000, "BBB", dur=2.5, issuer="Эмитент"), _row("E4", 1500, "BBB", dur=1.5, issuer="Эмитент")]
    other = [_row("O1", 700, "BBB", dur=1.0, issuer="Другой"), _row("O2", 1200, "BBB", dur=1.0, issuer="Другой")]
    st = issuer_curves(iss + other + [_row("S1", 500, "A", issuer="Одиночка")])
    e4 = st["E4"]
    assert e4.method == "fit" and abs(e4.ref - 900) < 1e-6 and abs(e4.resid - 600) < 1e-6 and e4.n_other == 3   # кривая по E1–E3: 800 + 100·(dur−0.5)
    assert st["E2"].method == "fit" and -300 < st["E2"].resid < 0                                               # кривая по E1,E3,E4 (E4 тянет её вверх): E2 ниже ориентира
    assert st["O1"].method == "single" and st["O1"].ref == 1200 and st["O1"].resid == -500
    assert "S1" not in st and "к ориентиру 900" in e4.describe()


def test_spread_history_stats_and_regimes():
    from datetime import date, timedelta
    from bondtrader.analytics.history import spread_stats
    settle = date(2025, 6, 2)
    flat = [(settle - timedelta(days=i), 500 + (i % 3) * 5) for i in range(1, 80)]
    st = spread_stats(flat, 520, settle, 90)
    assert st.n == 79 and abs(st.median - 505) <= 5 and st.chg30 is not None and abs(st.chg30 - 15) <= 10 and st.regime == "стабильно"
    assert spread_stats(flat[:5], 520, settle) is None                                                       # мало точек — нет статистики
    widened = spread_stats(flat, 1200, settle, 90)
    assert widened.z == 5.0 and widened.pct_rank == 1.0 and widened.regime == "расширение" and widened.chg60 is not None and widened.chg60 > 600
    tight = spread_stats(flat, 200, settle, 90)
    assert tight.regime == "сжатие" and tight.z == -5.0
    fresh_pts = [(settle - timedelta(days=i), 900 - i * 2) for i in range(1, 15)]                              # сделки только последние 2 недели
    fresh = spread_stats(fresh_pts, 880, settle, 90)
    assert fresh.fresh and fresh.regime == "первичка" and fresh.chg30 is None and "первичка" in fresh.describe()


def test_spread_history_service_uses_curve_of_the_day(tmp_path):
    from datetime import date, timedelta
    from bondtrader.data.history import SpreadHistoryService, ZcycStore
    from bondtrader.analytics.curve import ZeroCurve
    from bondtrader.models import Bond, Quote
    settle = date(2025, 6, 2)

    class FakeClient:
        def __init__(self):
            self.zcyc_calls = 0
        def history(self, secid, board, start, end):
            # доходность 25% → спред к кривой 15% = 1000 б.п.; выходной 2025-05-31 без сделки (YIELDCLOSE None)
            return [{"date": settle - timedelta(days=i), "ytm": 25.0, "duration": 1.0, "close": 100.0} for i in range(1, 40)] \
                + [{"date": settle - timedelta(days=2), "ytm": None, "duration": 1.0, "close": None}]
        def zcyc(self, on):
            self.zcyc_calls += 1
            if on.weekday() >= 5:      # MOEX на выходной отдаёт кривую последнего торгового дня
                on = on - timedelta(days=on.weekday() - 4)
            return {"yearyields": {"columns": ["tradedate", "period", "value"], "data": [[on.isoformat(), 0.5, 15.0], [on.isoformat(), 2.0, 15.0]]}}

    client = FakeClient()
    store = ZcycStore(str(tmp_path / "zcyc.json"))
    today = ZeroCurve(settle, [(0.5, 14.0), (2.0, 14.0)])
    svc = SpreadHistoryService(client, settle, days=60, store=store, today_curve=today)
    bond, quote = Bond("B1", name="B1", board="TQCB"), Quote("B1", settle, price=100.0, ytm_moex=26.0, duration_moex=1.0)
    pts = svc.series(bond)
    assert pts and all(abs(sp - 1000) < 1e-6 for _, sp in pts) and all(d.weekday() < 5 for d, _ in pts)   # выходные не в ряду: их кривая — чужая дата
    st = svc.stats(bond, quote, our_spread=999.0)
    assert st is not None and abs(st.now - 1200) < 1e-6 and abs(st.chg30 - 200) < 1e-6                # «сегодня» в методике MOEX: 26% − 14% = 1200
    svc.save()
    store2 = ZcycStore(str(tmp_path / "zcyc.json"))
    assert store2.get(settle - timedelta(days=3)) is not None and store2.get(settle - timedelta(days=1)) is None   # пт 30.05 в книге, вс 01.06 — нет
    calls = client.zcyc_calls
    svc2 = SpreadHistoryService(client, settle, days=60, store=store2, today_curve=today)
    svc2.series(Bond("B2", name="B2", board="TQCB"))
    assert client.zcyc_calls == calls                                                                 # кривые взяты из книги, MOEX не спрашивали


def test_gspread_history_and_issuer_ranks():
    from datetime import date
    from bondtrader.analytics.history import SpreadStats
    from bondtrader.analytics.issuer_curve import issuer_curves
    from bondtrader.portfolio import Portfolio
    from bondtrader.strategies import MarketContext, make_strategy
    rows = [_row("E1", 800, "BBB", dur=0.5, issuer="Эмитент"), _row("E2", 900, "BBB", dur=1.5, issuer="Эмитент"),
            _row("E3", 950, "BBB", dur=2.5, issuer="Эмитент"), _row("E4", 1500, "BBB", dur=1.5, issuer="Эмитент"), _row("S1", 2000, "B", issuer="Одиночка")]
    hs = {"S1": SpreadStats(40, date(2025, 3, 1), date(2025, 6, 1), 2000, 1900, 50, 2.0, 1800, 2000, 1.0, 50, 150, False, 90),
          "E1": SpreadStats(40, date(2025, 3, 1), date(2025, 6, 1), 800, 500, 40, 5.0, 480, 800, 1.0, 300, 320, False, 90)}
    ctx = MarketContext(date(2025, 6, 2), rows, None, None, Portfolio(cash=1e6), {}, hs, issuer_curves(rows))
    st = make_strategy("gspread", {"top_n": 5, "rank": "history", "min_excess_bp": 100, "per_issuer": 0})
    assert list(st.targets(ctx)) == ["E1"]                       # только расширение ≥100 за 30 дн.; E2–E4 без истории не участвуют
    assert "история: спред 800 при медиане 500" in st.explain(ctx)["E1"] and "эмитент:" in st.explain(ctx)["E1"]
    st = make_strategy("gspread", {"top_n": 5, "rank": "issuer", "min_excess_bp": 100, "per_issuer": 0})
    assert list(st.targets(ctx)) == ["E4"]                       # S1 без второго выпуска не участвует, E4 +600 к кривой эмитента
    assert "история: нет данных" in st.explain(ctx)["E4"]
    import pytest
    with pytest.raises(ValueError):
        make_strategy("gspread", {"rank": "magic"})


def test_spread_history_own_math_matches_screen_metrics(tmp_path):
    """История по цене закрытия и нашему графику потоков: та же формула, что в скрине, без «доходности к оферте» MOEX."""
    import json, os
    from datetime import date, timedelta
    from bondtrader.analytics.bond_math import compute_metrics
    from bondtrader.analytics.curve import ZeroCurve
    from bondtrader.data.history import SpreadHistoryService, ZcycStore
    from bondtrader.data.moex import apply_bondization, parse_bondization
    from bondtrader.market import _load_fixtures
    from bondtrader.models import Quote
    fix = os.path.join(os.path.dirname(__file__), "fixtures")
    snap = _load_fixtures(fix)
    bond, quote = next((b, q) for b, q in snap.universe if b.secid == "RU000A104ZK2")          # МТС 1P-21, оферта 2026-09-01
    with open(os.path.join(fix, "bondization_mts.json")) as f:
        bond = apply_bondization(bond, parse_bondization(json.load(f)), snap.settle)
    assert bond.has_full_schedule
    settle = snap.settle

    class FakeClient:
        def history(self, secid, board, start, end):
            # цена растёт на 0.1 в день → спред сжимается; MOEX-доходность нарочно абсурдная — использоваться не должна
            return [{"date": settle - timedelta(days=i), "close": quote.price - 0.1 * i, "accrued": 0.0, "face": 1000.0,
                     "ytm": 300.0, "duration": 0.01} for i in range(1, 45)]
        def zcyc(self, on):
            return {"yearyields": {"columns": ["tradedate", "period", "value"], "data": [[on.isoformat(), t, y] for t, y in snap.curve.points]}}

    svc = SpreadHistoryService(FakeClient(), settle, days=60, store=ZcycStore(""), today_curve=snap.curve)
    pts = svc.series(bond)
    assert svc.method(bond.secid) == "own" and len(pts) == 44
    d, sp = pts[-1]
    m = compute_metrics(bond, Quote(bond.secid, d, price=quote.price - 0.1, accrued=0.0), d, snap.curve)
    assert abs(sp - m.g_spread) < 1e-9 and all(abs(x) < 5000 for _, x in pts)                    # никаких «30000 б.п.» от оферты
    our = compute_metrics(bond, quote, settle, snap.curve).g_spread
    st = svc.stats(bond, Quote(bond.secid, settle, price=quote.price, ytm_moex=300.0, duration_moex=0.01), our)
    assert st is not None and abs(st.now - our) < 1e-9 and st.chg30 is not None and st.chg30 < 0     # «сегодня» — наш спред, не MOEX


def test_history_near_offer_is_flagged_and_excluded_from_history_rank():
    from datetime import date, timedelta
    from bondtrader.analytics.history import SpreadStats
    from bondtrader.data.history import SpreadHistoryService, ZcycStore
    from bondtrader.models import Bond, Quote
    from bondtrader.portfolio import Portfolio
    from bondtrader.strategies import MarketContext, make_strategy
    settle = date(2025, 6, 2)

    class FakeClient:
        def history(self, secid, board, start, end):
            return [{"date": settle - timedelta(days=i), "ytm": 25.0, "duration": 1.0, "close": 100.0} for i in range(1, 40)]
        def zcyc(self, on):
            return {"yearyields": {"columns": ["tradedate", "period", "value"], "data": [[on.isoformat(), 0.5, 15.0], [on.isoformat(), 2.0, 15.0]]}}
    svc = SpreadHistoryService(FakeClient(), settle, days=60, store=ZcycStore(""))
    soon = Bond("O1", name="O1", board="TQCB", offer_date=settle + timedelta(days=30))
    far = Bond("O2", name="O2", board="TQCB", offer_date=settle + timedelta(days=400))
    st_soon, st_far = svc.stats(soon, Quote("O1", settle, price=100.0), 1500.0), svc.stats(far, Quote("O2", settle, price=100.0), 1500.0)
    assert st_soon.note.startswith("оферта") and st_soon.regime == "оферта" and not st_soon.reliable and "ненадёжно" in st_soon.describe()
    assert st_far.reliable and st_far.regime == "расширение"
    rows = [_row("O1", 1500, "BB"), _row("O2", 1500, "BB")]
    ctx = MarketContext(settle, rows, None, None, Portfolio(cash=1e6), {}, {"O1": st_soon, "O2": st_far}, {})
    st = make_strategy("gspread", {"top_n": 5, "rank": "history", "min_excess_bp": 100, "per_issuer": 0})
    assert list(st.targets(ctx)) == ["O2"]


def test_peer_median_ignores_stressed_names():
    """Группа A-: половина пиров под стрессом (900–1850 б.п.) — ориентир считается по здоровой части (≈400),
    иначе ИЭК/Софтлайн на 390–410 выглядят «дешевле пиров»; стрессовые остаются в pct_rank."""
    from datetime import date
    from bondtrader.analytics.peers import peer_stats, trim_stressed
    from bondtrader.data.ratings import Rating
    from bondtrader.models import Bond, BondMetrics, Quote
    from bondtrader.screener import ScreenRow

    assert trim_stressed([386, 396, 406, 495, 564, 890, 928, 972, 985, 1003, 1413, 1432, 1847]) == [386, 396, 406, 495, 564]
    assert trim_stressed([300, 320, 350]) == [300, 320, 350]                  # мало пиров — не трогаем
    assert trim_stressed([1000, 1100, 1200, 1500, 1900, 2100]) == [1000, 1100, 1200, 1500, 1900, 2100]   # ВДО: разброс широкий, но не кратный

    def row(secid, spread, dur=1.5):
        b = Bond(secid, name=secid)
        q = Quote(secid, date(2025, 6, 2), price=100.0, turnover=5e6)
        m = BondMetrics(secid, 100.0, 1000.0, 20.0, None, 20.0, dur, dur * 0.9, 0, 0.1, 1.5, 15, spread)
        return ScreenRow(b, q, m, rating=Rating("x", "y", "A-"), sector="other")
    uni = [row("IEK", 406), row("S1", 386), row("S2", 396), row("S3", 495), row("S4", 564),
           row("D1", 890), row("D2", 972), row("D3", 1003), row("D4", 1413), row("D5", 1847)]
    ps = peer_stats(uni[0], uni, min_peers=5, dur_window=1.0)
    assert ps.trimmed == 5 and ps.n == 4 and ps.median == 445.5 and ps.excess == 406 - 445.5 and ps.group.endswith("без 5 стрессовых")
    assert ps.pct_rank == 2 / 9                                               # среди всех девяти пиров дешевле только двое
