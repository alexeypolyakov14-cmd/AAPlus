"""«Лестница» по срокам: равные доли по корзинам дюрации, минимум оборота."""
from __future__ import annotations

from .base import MarketContext, Strategy, duration_bucket


class LadderStrategy(Strategy):
    """Равновзвешенная лестница из N корзин дюрации.

    Параметры:
      edges        — правые границы корзин в годах, напр. [1, 2, 3, 5] -> 5 корзин (последняя 5+)
      per_bucket   — сколько бумаг держим в каждой корзине
      ofz_only     — только ОФЗ
      keep_held    — не менять бумаги, которые всё ещё проходят скрин и остались в своей корзине
      max_duration — верхняя граница дюрации (бумаги длиннее не берём)
    """
    name = "ladder"

    def __init__(self, edges=None, per_bucket: int = 2, ofz_only: bool = False,
                 keep_held: bool = True, max_duration: float = 7.0):
        self.edges = list(edges or [1.0, 2.0, 3.0, 5.0])
        self.per_bucket = per_bucket
        self.ofz_only = ofz_only
        self.keep_held = keep_held
        self.max_duration = max_duration
        self._reasons: dict[str, str] = {}

    def targets(self, ctx: MarketContext) -> dict[str, float]:
        self._reasons = {}
        n_buckets = len(self.edges) + 1
        candidates = [r for r in ctx.rows if r.metrics.macaulay_duration <= self.max_duration
                      and (r.bond.is_ofz or not self.ofz_only)]
        groups: dict[int, list] = {i: [] for i in range(n_buckets)}
        for r in candidates:
            groups[duration_bucket(r.metrics.macaulay_duration, self.edges)].append(r)
        held = ctx.held()
        chosen: dict[int, list[str]] = {}
        for i in range(n_buckets):
            rows = sorted(groups[i], key=lambda r: r.score, reverse=True)
            picks: list[str] = []
            if self.keep_held:
                picks = [r.secid for r in rows if r.secid in held][: self.per_bucket]
            for r in rows:
                if len(picks) >= self.per_bucket:
                    break
                if r.secid not in picks:
                    picks.append(r.secid)
            chosen[i] = picks
            lo = self.edges[i - 1] if i > 0 else 0.0
            hi = self.edges[i] if i < len(self.edges) else None
            label = f"корзина {lo:.0f}–{hi:.0f} лет" if hi else f"корзина {lo:.0f}+ лет"
            for s in picks:
                self._reasons[s] = f"{label}, YTW {ctx.by_id[s].metrics.yield_worst:.1f}%"
        filled = [i for i in chosen if chosen[i]]
        if not filled:
            return {}
        per_bucket_w = 1.0 / n_buckets   # пустые корзины остаются в деньгах
        out: dict[str, float] = {}
        for i in filled:
            for s in chosen[i]:
                out[s] = per_bucket_w / len(chosen[i])
        return out

    def explain(self, ctx: MarketContext) -> dict[str, str]:
        return dict(self._reasons)
