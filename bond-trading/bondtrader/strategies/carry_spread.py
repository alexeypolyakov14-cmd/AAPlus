"""Carry + roll-down + возврат спреда: полная ожидаемая доходность за горизонт.

E[R] = YTW·h + roll-down + D_mod · (спред − медиана группы аналогов) · reversion / 100

Третье слагаемое — ожидаемый ценовой доход от сжатия (или расширения, если спред уже узкий) G-спреда
к медиане группы «корзина дюрации × уровень листинга» за горизонт: reversion — доля отклонения,
которая, как мы предполагаем, сойдёт за h лет (0 — чистый carry, 1 — полный возврат к медиане).
Кандидаты — корпораты с z-оценкой спреда ≥ entry_z (z по SpreadMeanReversionStrategy: история спреда
или кросс-секция с MAD) и спредом ≤ max_spread_bp; ОФЗ участвуют без спредовой надбавки.
"""
from __future__ import annotations

import statistics

from ..analytics.bond_math import rolldown_return
from .base import MarketContext, duration_bucket
from .carry import CarryRollDownStrategy
from .spread import SpreadMeanReversionStrategy


class CarrySpreadStrategy(CarryRollDownStrategy):
    """Carry + roll-down + ожидаемое сжатие G-спреда к медиане аналогов (корзина дюрации × листинг) за горизонт."""
    name = "carry_spread"

    def __init__(self, top_n: int = 12, horizon: float = 1.0, max_duration: float = 5.0,
                 per_unit_duration: bool = False, ofz_min_share: float = 0.4, min_expected: float = 0.0,
                 reversion: float = 0.5, entry_z: float = -99.0, max_spread_bp: float = 1000.0,
                 edges=None, min_group: int = 6, z_cap: float = 5.0, min_history: int = 40):
        super().__init__(top_n=top_n, horizon=horizon, max_duration=max_duration,
                         per_unit_duration=per_unit_duration, ofz_min_share=ofz_min_share, min_expected=min_expected)
        self.reversion = reversion
        self.entry_z = entry_z
        self.max_spread_bp = max_spread_bp
        self.edges = list(edges or [1.0, 2.0, 3.0, 5.0])
        self.min_group = min_group
        self._spread = SpreadMeanReversionStrategy(entry_z=entry_z, top_n=top_n, min_history=min_history,
                                                   edges=self.edges, max_spread_bp=max_spread_bp,
                                                   min_group=min_group, z_cap=z_cap)
        self._parts: dict[str, tuple[float, float, float, float, float]] = {}   # secid -> (carry, roll, excess_bp, z, spread_gain)

    def excess_spreads(self, ctx: MarketContext) -> dict[str, float]:
        """Отклонение G-спреда от медианы группы (корзина дюрации × уровень листинга), б.п."""
        corp = [r for r in ctx.rows if not r.bond.is_ofz and r.metrics.g_spread is not None]
        groups: dict[tuple, list] = {}
        for r in corp:
            groups.setdefault((duration_bucket(r.metrics.macaulay_duration, self.edges), r.bond.list_level or 0), []).append(r)
        all_med = statistics.median([r.metrics.g_spread for r in corp]) if corp else 0.0
        out: dict[str, float] = {}
        for rows in groups.values():
            med = statistics.median([r.metrics.g_spread for r in rows]) if len(rows) >= self.min_group else all_med
            for r in rows:
                out[r.secid] = r.metrics.g_spread - med
        return out

    def expected_returns(self, ctx: MarketContext) -> dict[str, float]:
        self._parts = {}
        z = self._spread.zscores(ctx)
        excess = self.excess_spreads(ctx)
        out: dict[str, float] = {}
        for r in ctx.rows:
            m = r.metrics
            if m.macaulay_duration > self.max_duration or r.bond.is_floater:
                continue
            roll = rolldown_return(ctx.curve, m.macaulay_duration, self.horizon) if ctx.curve else 0.0
            carry = m.yield_worst * self.horizon
            gain, ex, zz = 0.0, 0.0, 0.0
            if not r.bond.is_ofz:
                if m.g_spread is None or m.g_spread > self.max_spread_bp:
                    continue
                zz = z.get(r.secid, 0.0)
                if zz < self.entry_z:
                    continue
                ex = excess.get(r.secid, 0.0)
                gain = m.modified_duration * ex * self.reversion / 100.0
            er = carry + roll + gain
            if self.per_unit_duration:
                er = er / max(m.modified_duration, 0.25)
            out[r.secid] = er
            self._parts[r.secid] = (carry, roll, ex, zz, gain)
        return out

    def targets(self, ctx: MarketContext) -> dict[str, float]:
        w = super().targets(ctx)
        for s in w:
            carry, roll, ex, zz, gain = self._parts.get(s, (0.0, 0.0, 0.0, 0.0, 0.0))
            er = carry + roll + gain
            if ctx.by_id[s].bond.is_ofz:
                self._reasons[s] = f"YTW {carry:.1f}% + roll-down {roll:.2f}% = E[R] {er:.2f}% за {self.horizon:g} г. (ОФЗ, без спреда)"
            else:
                self._reasons[s] = (f"YTW {carry:.1f}% + roll-down {roll:.2f}% + спред {ex:+.0f} б.п. к аналогам (z {zz:+.1f}) "
                                    f"× возврат {self.reversion:g} = {gain:+.2f}% → E[R] {er:.2f}% за {self.horizon:g} г.")
        return w
