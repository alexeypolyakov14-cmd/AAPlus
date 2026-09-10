"""Возврат кредитных спредов к среднему (корпоративные облигации)."""
from __future__ import annotations

import statistics

from .base import MarketContext, Strategy, duration_bucket, equal_weights


class SpreadMeanReversionStrategy(Strategy):
    """Покупаем корпораты с аномально широким G-спредом относительно аналогов, продаём при сжатии.

    Z-оценка спреда:
      • временная — если в ctx.spread_history есть ≥ min_history наблюдений по бумаге:
        z = (spread − mean) / std;
      • иначе кросс-секционная — относительно медианы группы (корзина дюрации × уровень листинга)
        с робастным масштабом MAD.
    Вход: z ≥ entry_z, выход: z ≤ exit_z. Не более top_n позиций, равные веса, доля ОФЗ-«якоря» ofz_anchor.
    """
    name = "spread"

    def __init__(self, entry_z: float = 1.0, exit_z: float = 0.0, top_n: int = 8, min_history: int = 40,
                 edges=None, ofz_anchor: float = 0.3, max_spread_bp: float = 600):
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.top_n = top_n
        self.min_history = min_history
        self.edges = list(edges or [1.0, 2.0, 3.0, 5.0])
        self.ofz_anchor = ofz_anchor
        self.max_spread_bp = max_spread_bp
        self._reasons: dict[str, str] = {}

    def zscores(self, ctx: MarketContext) -> dict[str, float]:
        corp = [r for r in ctx.rows if not r.bond.is_ofz and r.metrics.g_spread is not None]
        groups: dict[tuple, list] = {}
        for r in corp:
            key = (duration_bucket(r.metrics.macaulay_duration, self.edges), r.bond.list_level or 0)
            groups.setdefault(key, []).append(r)
        z: dict[str, float] = {}
        for key, rows in groups.items():
            spreads = [r.metrics.g_spread for r in rows]
            med = statistics.median(spreads)
            mad = statistics.median(abs(s - med) for s in spreads) * 1.4826 if len(spreads) > 2 else 0.0
            for r in rows:
                hist = ctx.spread_history.get(r.secid)
                if hist and len(hist) >= self.min_history:
                    mu = statistics.fmean(hist)
                    sd = statistics.pstdev(hist)
                    z[r.secid] = (r.metrics.g_spread - mu) / sd if sd > 1e-9 else 0.0
                elif mad > 1e-9:
                    z[r.secid] = (r.metrics.g_spread - med) / mad
                else:
                    z[r.secid] = 0.0
        return z

    def targets(self, ctx: MarketContext) -> dict[str, float]:
        self._reasons = {}
        z = self.zscores(ctx)
        by_id = ctx.by_id
        held = ctx.held()
        keep = [s for s in held if s in z and z[s] > self.exit_z
                and by_id[s].metrics.g_spread <= self.max_spread_bp]
        for s in held:
            if s in z and s not in keep:
                self._reasons[s] = f"спред сжался: z={z[s]:.2f} ≤ {self.exit_z}"
        new = [s for s, v in sorted(z.items(), key=lambda kv: -kv[1])
               if s not in held and v >= self.entry_z and by_id[s].metrics.g_spread <= self.max_spread_bp]
        picks = keep + new
        picks = picks[: self.top_n]
        for s in picks:
            self._reasons.setdefault(s, f"G-спред {by_id[s].metrics.g_spread:.0f} б.п., z={z[s]:.2f}")
        out = equal_weights(picks, 1.0 - self.ofz_anchor) if picks else {}
        # якорь — самая ликвидная ОФЗ с дюрацией 1–3 года
        ofz = [r for r in ctx.rows if r.bond.is_ofz and 1.0 <= r.metrics.macaulay_duration <= 3.0]
        if ofz and self.ofz_anchor > 0:
            anchor = max(ofz, key=lambda r: r.quote.turnover)
            out[anchor.secid] = out.get(anchor.secid, 0.0) + self.ofz_anchor
            self._reasons[anchor.secid] = "ОФЗ-якорь ликвидности"
        return out

    def explain(self, ctx: MarketContext) -> dict[str, str]:
        return dict(self._reasons)
