"""Carry + roll-down: максимизация ожидаемой доходности за горизонт при ограничении дюрации."""
from __future__ import annotations

from ..analytics.bond_math import rolldown_return
from .base import MarketContext, Strategy


class CarryRollDownStrategy(Strategy):
    """E[R] = доходность к худшему + доход от скатывания по кривой за horizon лет.

    Ранжируем по E[R] (опционально на единицу дюрации), берём top_n, веса ∝ E[R].
    Минимальная доля ОФЗ ofz_min_share гарантирует ликвидное ядро.
    """
    name = "carry"

    def __init__(self, top_n: int = 10, horizon: float = 1.0, max_duration: float = 5.0,
                 per_unit_duration: bool = False, ofz_min_share: float = 0.4, min_expected: float = 0.0):
        self.top_n = top_n
        self.horizon = horizon
        self.max_duration = max_duration
        self.per_unit_duration = per_unit_duration
        self.ofz_min_share = ofz_min_share
        self.min_expected = min_expected
        self._reasons: dict[str, str] = {}

    def expected_returns(self, ctx: MarketContext) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in ctx.rows:
            m = r.metrics
            if m.macaulay_duration > self.max_duration or r.bond.is_floater:
                continue
            roll = rolldown_return(ctx.curve, m.macaulay_duration, self.horizon) if ctx.curve else 0.0
            er = m.yield_worst * self.horizon + roll
            if self.per_unit_duration:
                er = er / max(m.modified_duration, 0.25)
            out[r.secid] = er
        return out

    def targets(self, ctx: MarketContext) -> dict[str, float]:
        self._reasons = {}
        er = self.expected_returns(ctx)
        by_id = ctx.by_id
        ranked = [s for s, v in sorted(er.items(), key=lambda kv: -kv[1]) if v >= self.min_expected]
        picks = ranked[: self.top_n]
        if not picks:
            return {}
        # гарантируем долю ОФЗ
        ofz_in = [s for s in picks if by_id[s].bond.is_ofz]
        if not ofz_in and self.ofz_min_share > 0:
            ofz_ranked = [s for s in ranked if by_id[s].bond.is_ofz]
            if ofz_ranked:
                picks[-1] = ofz_ranked[0]
                ofz_in = [ofz_ranked[0]]
        total = sum(er[s] for s in picks)
        w = {s: er[s] / total for s in picks}
        ofz_share = sum(w[s] for s in ofz_in)
        if ofz_in and ofz_share < self.ofz_min_share:
            k_ofz = self.ofz_min_share / ofz_share
            k_corp = (1 - self.ofz_min_share) / (1 - ofz_share) if ofz_share < 1 else 0
            w = {s: (w[s] * k_ofz if s in ofz_in else w[s] * k_corp) for s in picks}
        for s in picks:
            m = by_id[s].metrics
            roll = rolldown_return(ctx.curve, m.macaulay_duration, self.horizon) if ctx.curve else 0.0
            self._reasons[s] = f"YTW {m.yield_worst:.1f}% + roll-down {roll:.2f}% = E[R] {er[s]:.2f}% за {self.horizon:g} г."
        return w

    def explain(self, ctx: MarketContext) -> dict[str, str]:
        return dict(self._reasons)
