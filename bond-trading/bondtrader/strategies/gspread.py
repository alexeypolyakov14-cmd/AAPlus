"""gspread: ранжирование только по спреду к кривой ОФЗ (G-curve).

Никаких моделей: после сита ликвидности и стоп-факторов берём top_n бумаг с наибольшим G-спредом.
Опционально не больше per_issuer выпусков одного эмитента (у ВДО-эмитентов часто по 3–5 выпусков подряд)
и потолок дюрации. Веса равные, плюс ликвидное ядро в ОФЗ ofz_min_share.
"""
from __future__ import annotations

from .base import MarketContext, Strategy


class GSpreadStrategy(Strategy):
    """Только G-curve: top_n корпоратов с наибольшим спредом к кривой ОФЗ после сита и стоп-факторов, равные веса."""
    name = "gspread"

    def __init__(self, top_n: int = 10, per_issuer: int = 1, max_duration: float = 3.0, min_spread_bp: float = 0.0,
                 ofz_min_share: float = 0.0):
        self.top_n, self.per_issuer, self.max_duration = top_n, per_issuer, max_duration
        self.min_spread_bp, self.ofz_min_share = min_spread_bp, ofz_min_share
        self._reasons: dict[str, str] = {}

    def ranked(self, ctx: MarketContext) -> list:
        rows = [r for r in ctx.rows if not r.bond.is_ofz and not r.bond.is_floater and r.metrics.g_spread is not None
                and r.metrics.macaulay_duration <= self.max_duration and r.metrics.g_spread >= self.min_spread_bp]
        rows.sort(key=lambda r: -r.metrics.g_spread)
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
            self._reasons[r.secid] = (f"G-спред {m.g_spread:+.0f} б.п. (YTW {m.yield_worst:.1f}% при ОФЗ {m.yield_worst - m.g_spread / 100:.1f}% "
                                      f"на дюрации {m.macaulay_duration:.1f}), рейтинг {r.rating_str}")
        if ofz and self.ofz_min_share > 0:
            w[ofz[0].secid] = self.ofz_min_share if picks else 1.0
            self._reasons[ofz[0].secid] = "ликвидное ядро в ОФЗ"
        return w

    def explain(self, ctx: MarketContext) -> dict[str, str]:
        return dict(self._reasons)
