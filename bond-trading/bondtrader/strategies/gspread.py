"""gspread: ранжирование по спреду к кривой ОФЗ (G-curve) без моделей.

rank = "spread" — сырой G-спред: кому рынок доверяет меньше всего.
rank = "peers"  — превышение над медианой своей ступени рейтинга (без рейтинга — своя группа): за что платят больше,
                  чем за соседей по рейтингу; min_excess_bp отсекает тех, кто платит не больше соседей.
rank = "model"  — остаток к регрессии справедливого спреда (рейтинг, дюрация, оборот, листинг).
После сита ликвидности и стоп-факторов берём top_n, не больше per_issuer выпусков одного эмитента,
потолок дюрации max_duration. Веса равные, плюс ликвидное ядро в ОФЗ ofz_min_share.
"""
from __future__ import annotations

import statistics

from .base import MarketContext, Strategy


class GSpreadStrategy(Strategy):
    """Только G-curve: top_n корпоратов с наибольшим спредом к кривой ОФЗ после сита и стоп-факторов, равные веса."""
    name = "gspread"

    def __init__(self, top_n: int = 10, per_issuer: int = 1, max_duration: float = 3.0, min_spread_bp: float = 0.0,
                 ofz_min_share: float = 0.0, rank: str = "spread", min_excess_bp: float = 0.0, min_peers: int = 3):
        self.top_n, self.per_issuer, self.max_duration = top_n, per_issuer, max_duration
        self.min_spread_bp, self.ofz_min_share = min_spread_bp, ofz_min_share
        self.rank, self.min_excess_bp, self.min_peers = rank, min_excess_bp, min_peers
        self._reasons: dict[str, str] = {}
        self.excess: dict[str, float] = {}     # секид -> превышение над соседями/моделью (б.п.)
        self.peer_median: dict[str, float] = {}

    def _excess(self, universe: list) -> dict[str, float]:
        """Превышение спреда над ориентиром: медиана ступени (peers) или регрессия (model). Для spread — сам спред."""
        if self.rank == "model":
            from ..analytics.fair_spread import features_of, fit_fair_spread
            model = fit_fair_spread(universe)
            return {r.secid: (model.residual(features_of(r), r.metrics.g_spread) if model.ok else r.metrics.g_spread) for r in universe}
        if self.rank == "peers":
            buckets: dict[str, list[float]] = {}
            for r in universe:
                buckets.setdefault(r.rating.rating if r.rating else "—", []).append(r.metrics.g_spread)
            self.peer_median = {g: statistics.median(v) for g, v in buckets.items()}
            out = {}
            for r in universe:
                g = r.rating.rating if r.rating else "—"
                # в ступени слишком мало бумаг — медиана не показательна, сравниваем с общей медианой корпоратов
                med = self.peer_median[g] if len(buckets[g]) >= self.min_peers else statistics.median(x.metrics.g_spread for x in universe)
                out[r.secid] = r.metrics.g_spread - med
            return out
        return {r.secid: r.metrics.g_spread for r in universe}

    def ranked(self, ctx: MarketContext) -> list:
        # ориентир (медиана ступени / модель) считается по всему корпоративному срезу, а не только по коротким бумагам
        universe = [r for r in ctx.rows if not r.bond.is_ofz and not r.bond.is_floater and r.metrics.g_spread is not None]
        self.excess = self._excess(universe)
        rows = [r for r in universe if r.metrics.macaulay_duration <= self.max_duration and r.metrics.g_spread >= self.min_spread_bp
                and (self.rank == "spread" or self.excess[r.secid] >= self.min_excess_bp)]
        rows.sort(key=lambda r: -self.excess[r.secid])
        picks, per = [], {}
        for r in rows:
            k = r.bond.issuer_key
            if self.per_issuer and per.get(k, 0) >= self.per_issuer:
                continue
            per[k] = per.get(k, 0) + 1
            picks.append(r)
            if len(picks) >= self.top_n:
                break
        return picks

    def targets(self, ctx: MarketContext) -> dict[str, float]:
        self._reasons = {}
        picks = self.ranked(ctx)
        ofz = sorted((r for r in ctx.rows if r.bond.is_ofz and not r.bond.is_floater and not r.bond.is_linker),
                     key=lambda r: abs(r.metrics.macaulay_duration - 1.0))
        w: dict[str, float] = {}
        corp_share = 1.0 - (self.ofz_min_share if ofz and self.ofz_min_share > 0 else 0.0)
        for r in picks:
            w[r.secid] = corp_share / len(picks)
            m = r.metrics
            base = (f"G-спред {m.g_spread:+.0f} б.п. (YTW {m.yield_worst:.1f}% при ОФЗ {m.yield_worst - m.g_spread / 100:.1f}% "
                    f"на дюрации {m.macaulay_duration:.1f}), рейтинг {r.rating_str}")
            if self.rank == "peers":
                g = r.rating.rating if r.rating else "—"
                base += f"; +{self.excess[r.secid]:.0f} б.п. к медиане ступени {g} ({self.peer_median.get(g, 0):.0f})"
            elif self.rank == "model":
                base += f"; {self.excess[r.secid]:+.0f} б.п. к справедливому"
            self._reasons[r.secid] = base
        if ofz and self.ofz_min_share > 0:
            w[ofz[0].secid] = self.ofz_min_share if picks else 1.0
            self._reasons[ofz[0].secid] = "ликвидное ядро в ОФЗ"
        return w

    def explain(self, ctx: MarketContext) -> dict[str, str]:
        return dict(self._reasons)
