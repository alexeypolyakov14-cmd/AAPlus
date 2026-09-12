"""Дневной событийный бэктест облигационных стратегий.

Учитывается: чистая цена + НКД, купоны и амортизации по графику bondization,
погашение по номиналу, комиссия и проскальзывание, периодическая ребалансировка.
Ограничения: исполнение по цене закрытия дня ребалансировки; нет учёта налогов;
вселенная задаётся заранее (риск survivorship bias — см. README).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from ..analytics.bond_math import coupon_payments_between
from ..analytics.curve import ZeroCurve
from ..data.cbr import KeyRateView, analyze_keyrate
from ..models import Bond, Quote
from ..portfolio import Fill, Order, Portfolio
from ..risk import RiskLimits, RiskManager, orders_from_targets
from ..screener import Screener, ScreenerConfig, ScreenRow
from ..strategies.base import MarketContext, Strategy
from .data import HistoryProvider
from .metrics import PerformanceStats, performance

log = logging.getLogger(__name__)


@dataclass
class Trade:
    date: date
    secid: str
    side: str
    qty: int
    price: float
    value: float
    commission: float
    reason: str = ""


@dataclass
class BacktestResult:
    nav: pd.Series
    trades: list[Trade]
    stats: PerformanceStats
    benchmark: Optional[pd.Series] = None
    weights_history: dict[date, dict[str, float]] = field(default_factory=dict)
    final_portfolio: Optional[Portfolio] = None
    coupons_received: float = 0.0
    commissions_paid: float = 0.0
    regimes: dict[date, str] = field(default_factory=dict)
    cash_income: float = 0.0
    avg_invested: float = 0.0

    def summary(self) -> dict:
        d = self.stats.as_dict()
        d.update({"trades": len(self.trades), "coupons_received": round(self.coupons_received, 2),
                  "cash_income": round(self.cash_income, 2), "avg_invested_pct": round(self.avg_invested * 100, 1),
                  "commissions_paid": round(self.commissions_paid, 2)})
        return d


def _is_rebalance_day(d: pd.Timestamp, prev: Optional[pd.Timestamp], freq: str) -> bool:
    if prev is None:
        return True
    if freq == "daily":
        return True
    if freq == "weekly":
        return d.isocalendar()[1] != prev.isocalendar()[1] or d.year != prev.year
    if freq == "monthly":
        return d.month != prev.month or d.year != prev.year
    if freq == "quarterly":
        return (d.month - 1) // 3 != (prev.month - 1) // 3 or d.year != prev.year
    raise ValueError(f"неизвестная частота ребалансировки {freq}")


class BacktestEngine:
    def __init__(self, strategy: Strategy, bonds: list[Bond], provider: HistoryProvider, start: date, end: date,
                 initial_cash: float = 1_000_000, commission_bp: float = 5, slippage_bp: float = 5,
                 rebalance: str = "monthly", screener_cfg: Optional[ScreenerConfig] = None,
                 risk_limits: Optional[RiskLimits] = None, benchmark: Optional[str] = "RGBITR",
                 spread_history_len: int = 60, cash_spread_bp: float = -50.0):
        self.strategy = strategy
        self.bonds = {b.secid: b for b in bonds}
        self.provider = provider
        self.start, self.end = start, end
        self.initial_cash = initial_cash
        self.commission = commission_bp / 10000
        self.slippage = slippage_bp / 10000
        self.rebalance = rebalance
        self.screener = Screener(screener_cfg or ScreenerConfig(min_turnover=0, max_bid_ask_pct=100))
        self.risk = RiskManager(risk_limits or RiskLimits())
        self.benchmark_name = benchmark
        self.spread_history_len = spread_history_len
        self.cash_spread_bp = cash_spread_bp   # доходность свободных денег = ключевая ставка + спред (фонд ликвидности/РЕПО)

    # ---- загрузка ----
    def _load(self) -> tuple[dict[str, pd.DataFrame], pd.DatetimeIndex]:
        hist: dict[str, pd.DataFrame] = {}
        all_dates: set = set()
        for secid, bond in self.bonds.items():
            self.provider.enrich(bond)
            df = self.provider.bond_history(bond, self.start - timedelta(days=10), self.end)
            df = df[df["close"].notna()] if "close" in df else df
            if df.empty:
                log.warning("%s: нет истории, исключена", secid)
                continue
            hist[secid] = df
            all_dates.update(df.index[df.index >= pd.Timestamp(self.start)])
        if not hist:
            raise ValueError("нет истории ни по одной бумаге")
        cal = pd.DatetimeIndex(sorted(all_dates))
        return hist, cal

    # ---- события по бумаге между датами (prev, today] ----
    def _process_cashflows(self, pf: Portfolio, bond: Bond, prev: Optional[date], today: date, last_face: float) -> None:
        pos = pf.positions.get(bond.secid)
        if not pos:
            return
        lo = prev or (today - timedelta(days=1))
        for _d, val in coupon_payments_between(bond, lo, today):
            pf.apply_coupon(bond.secid, val)
        if bond.has_full_schedule and bond.amortizations:
            for d, val in bond.amortizations:
                if lo < d <= today and val > 0:
                    full = bond.maturity is not None and d >= bond.maturity
                    pf.apply_redemption(bond.secid, val, full=full)
                    if bond.secid not in pf.positions:
                        return
        if bond.maturity and lo < bond.maturity <= today and bond.secid in pf.positions:
            pf.apply_redemption(bond.secid, last_face, full=True)

    def run(self) -> BacktestResult:
        hist, cal = self._load()
        keyrate = self.provider.keyrate_history(self.start - timedelta(days=400), self.end)
        pf = Portfolio(cash=self.initial_cash)
        nav_rows: list[tuple[pd.Timestamp, float]] = []
        trades: list[Trade] = []
        weights_hist: dict[date, dict[str, float]] = {}
        regimes: dict[date, str] = {}
        spread_hist: dict[str, list[float]] = {}
        prev_ts: Optional[pd.Timestamp] = None
        last_face: dict[str, float] = {}
        cash_income = 0.0
        invested_share: list[float] = []

        for ts in cal:
            today = ts.date()
            prev_day = prev_ts.date() if prev_ts is not None else None
            # 0) доход на свободные деньги за прошедшие календарные дни
            if prev_day is not None and pf.cash > 0:
                rate = self._keyrate_on(keyrate, today)
                if rate is not None:
                    r = max((rate + self.cash_spread_bp / 100) / 100, 0.0)  # деньги не могут приносить отрицательный доход
                    inc = pf.cash * ((1 + r) ** ((today - prev_day).days / 365) - 1)
                    pf.cash += inc
                    cash_income += inc
            # 1) купоны / амортизации / погашения
            for secid in list(pf.positions):
                bond = self.bonds[secid]
                self._process_cashflows(pf, bond, prev_day, today, last_face.get(secid, bond.face_value))

            # 2) котировки на сегодня
            universe: list[tuple[Bond, Quote]] = []
            marks: dict[str, tuple[float, float, float]] = {}
            stale: set[str] = set()   # позиции без сделок сегодня: держим по последней цене, не докупаем и не продаём «за выпадение»
            for secid, df in hist.items():
                bond = self.bonds[secid]
                if bond.maturity and bond.maturity <= today:
                    continue
                if ts not in df.index:
                    # бумага не торговалась — последняя известная цена для оценки; удерживаемая позиция остаётся во вселенной,
                    # иначе стратегия её «не видит» и движок продаёт как выпавшую из скрина (ложная ребалансировка на тонких днях)
                    sub = df.loc[:ts]
                    if sub.empty or secid not in pf.positions:
                        continue
                    row = sub.iloc[-1]
                    face = float(row["face"]) if pd.notna(row.get("face")) else bond.face_value
                    accrued = float(row["accrued"]) if pd.notna(row.get("accrued")) else 0.0
                    marks[secid] = (float(row["close"]), accrued, face)
                    universe.append((bond, Quote(secid, today, price=float(row["close"]), accrued=accrued,
                                                 ytm_moex=float(row["ytm"]) if pd.notna(row.get("ytm")) else None,
                                                 duration_moex=float(row["duration"]) if pd.notna(row.get("duration")) else None,
                                                 turnover=0.0)))
                    stale.add(secid)
                    continue
                row = df.loc[ts]
                face = float(row["face"]) if pd.notna(row.get("face")) else bond.face_value
                bond.face_value = face
                last_face[secid] = face
                accrued = float(row["accrued"]) if pd.notna(row.get("accrued")) else 0.0
                q = Quote(secid, today, price=float(row["close"]), accrued=accrued,
                          ytm_moex=float(row["ytm"]) if pd.notna(row.get("ytm")) else None,
                          duration_moex=float(row["duration"]) if pd.notna(row.get("duration")) else None,
                          turnover=float(row["value"]) if pd.notna(row.get("value")) else 0.0)
                universe.append((bond, q))
                marks[secid] = (q.price, accrued, face)

            nav = pf.nav(marks)
            nav_rows.append((ts, nav))
            if nav > 0:
                invested_share.append(1 - pf.cash / nav)

            # 3) ребалансировка
            if _is_rebalance_day(ts, prev_ts, self.rebalance) and universe:
                curve = self._curve(universe, today)
                rows = self.risk.eligible(self.screener.run(universe, curve, today))
                by_id = {r.secid: r for r in rows}
                for r in rows:
                    if r.metrics.g_spread is not None:
                        h = spread_hist.setdefault(r.secid, [])
                        h.append(r.metrics.g_spread)
                        if len(h) > self.spread_history_len:
                            del h[0]
                kr = self._keyrate_view(keyrate, today)
                ctx = MarketContext(today, rows, curve, kr, pf, {k: v[:-1] for k, v in spread_hist.items()})
                if kr:
                    regimes[today] = kr.regime
                try:
                    targets = self.strategy.targets(ctx)
                except Exception as e:  # noqa: BLE001
                    log.error("%s: стратегия упала: %s", today, e)
                    targets = {}
                targets, _notes = self.risk.enforce_targets(targets, by_id)
                # позиции, выпавшие из скрина, но всё ещё торгуемые — продаём по рынку
                sell_rows = dict(by_id)
                for secid in pf.positions:
                    if secid not in sell_rows and secid in marks:
                        sell_rows[secid] = self._synthetic_row(self.bonds[secid], marks[secid], today)
                orders = orders_from_targets(pf, targets, sell_rows, strategy=self.strategy.name, reasons=self.strategy.explain(ctx))
                for o in orders:
                    if o.secid in stale:
                        continue   # цена устарела: ни докупать, ни продавать по ней — ждём дня со сделками
                    self._execute(pf, o, sell_rows[o.secid], today, trades)
                weights_hist[today] = pf.weights(marks)
            prev_ts = ts

        nav_s = pd.Series(dict(nav_rows), dtype=float).sort_index()
        nav_s.name = "NAV"
        bench = None
        if self.benchmark_name:
            try:
                bench = self.provider.index_history(self.benchmark_name, self.start, self.end)
                if bench is not None and bench.empty:
                    log.warning("бенчмарк %s: история за %s — %s пуста", self.benchmark_name, self.start, self.end)
                    bench = None
            except Exception as e:  # noqa: BLE001
                log.warning("бенчмарк %s недоступен: %s", self.benchmark_name, e)
        rf = (sum(r for _, r in keyrate) / len(keyrate)) if keyrate else 0.0
        stats = performance(nav_s, rf_annual_pct=rf, benchmark=bench)
        return BacktestResult(nav_s, trades, stats, bench, weights_hist, pf, pf.coupons_received, pf.commissions_paid, regimes,
                              cash_income, (sum(invested_share) / len(invested_share)) if invested_share else 0.0)

    # ---- вспомогательные ----
    def _curve(self, universe: list[tuple[Bond, Quote]], today: date) -> Optional[ZeroCurve]:
        payload = self.provider.zcyc(today)
        if payload:
            try:
                return ZeroCurve.from_moex_zcyc(payload)
            except ValueError:
                pass
        items = [(q.duration_moex, q.ytm_moex) for b, q in universe
                 if b.is_ofz and not b.is_floater and not b.is_linker and q.duration_moex and q.ytm_moex]
        if len(items) < 2:
            from ..analytics.bond_math import compute_metrics
            items = []
            for b, q in universe:
                if b.is_ofz and not b.is_floater and not b.is_linker:
                    m = compute_metrics(b, q, today)
                    if m:
                        items.append((m.macaulay_duration, m.ytm))
        try:
            return ZeroCurve.from_ofz_metrics(items, today)
        except ValueError:
            return None

    @staticmethod
    def _keyrate_on(history: list[tuple[date, float]], today: date) -> Optional[float]:
        rate = None
        for d, r in history:
            if d <= today:
                rate = r
            else:
                break
        return rate

    @staticmethod
    def _keyrate_view(history: list[tuple[date, float]], today: date) -> Optional[KeyRateView]:
        upto = [(d, r) for d, r in history if d <= today]
        if not upto:
            return None
        if upto[-1][0] < today:
            upto = upto + [(today, upto[-1][1])]
        try:
            return analyze_keyrate(upto)
        except ValueError:
            return None

    @staticmethod
    def _synthetic_row(bond: Bond, mark: tuple[float, float, float], today: date) -> ScreenRow:
        from ..models import BondMetrics
        price, accrued, face = mark
        dirty = face * price / 100 + accrued
        m = BondMetrics(bond.secid, price, dirty, 0.0, None, 0.0, 0.0, 0.0, 0.0, 0.0,
                        max((bond.maturity - today).days / 365, 0.0) if bond.maturity else 0.0, 0.0, None)
        return ScreenRow(bond, Quote(bond.secid, today, price=price, accrued=accrued), m)

    def _execute(self, pf: Portfolio, o: Order, row: ScreenRow, today: date, trades: list[Trade]) -> None:
        px = row.metrics.clean_price * (1 + self.slippage if o.side == "BUY" else 1 - self.slippage)
        face = row.bond.face_value
        gross = o.qty * (face * px / 100 + row.quote.accrued)
        fee = gross * self.commission
        if o.side == "BUY" and gross + fee > pf.cash:
            qty = int((pf.cash / (1 + self.commission)) // (face * px / 100 + row.quote.accrued))
            if qty <= 0:
                return
            o.qty = qty
            gross = qty * (face * px / 100 + row.quote.accrued)
            fee = gross * self.commission
        pf.apply_fill(Fill(o.secid, o.side, o.qty, px, row.quote.accrued, face, fee, today))
        trades.append(Trade(today, o.secid, o.side, o.qty, px, gross, fee, o.reason))
