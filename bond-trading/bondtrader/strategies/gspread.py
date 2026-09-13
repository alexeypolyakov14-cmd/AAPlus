"""gspread: ранжирование по спреду к кривой ОФЗ (G-curve) без моделей.

rank = "spread"  — сырой G-спред: кому рынок доверяет меньше всего.
rank = "peers"   — превышение над медианой своей ступени рейтинга (без рейтинга — свой сектор): за что платят больше,
                   чем за соседей по рейтингу; min_excess_bp отсекает тех, кто платит не больше соседей.
rank = "model"   — остаток к регрессии справедливого спреда (рейтинг, дюрация, оборот, листинг).
rank = "history" — расширение спреда за 30 дней против собственной истории бумаги (ctx.history_stats): «что-то
                   случилось», а не «всегда так торговалась»; бумаги без истории и с офертой рядом (история ненадёжна)
                   не участвуют.
rank = "issuer"  — превышение над кривой самого эмитента (ctx.issuer_stats): один выпуск шире соседей по эмитенту;
                   эмитенты с одним выпуском не участвуют.
rank = "quality" — обе оси: превышение над пирами (как peers), но только у бумаг, чьи метрики из релиза агентства
                   не хуже выданной ступени (ctx.quality_stats, gap ≥ min_gap). Без метрик — не участвуют.
После сита ликвидности и стоп-факторов берём top_n, не больше per_issuer выпусков одного эмитента,
потолок дюрации max_duration. Веса равные, плюс ликвидное ядро в ОФЗ ofz_min_share.
Обоснование каждой бумаги всегда содержит все доступные ракурсы (пиры, история, кривая эмитента), какой бы
rank ни был выбран — чтобы списки разных методик можно было сравнивать по одной строке.
"""
from __future__ import annotations

from .base import MarketContext, Strategy

RANKS = ("spread", "peers", "model", "history", "issuer", "quality")


class GSpreadStrategy(Strategy):
    """Только G-curve: top_n корпоратов с наибольшим спредом к кривой ОФЗ после сита и стоп-факторов, равные веса."""
    name = "gspread"

    def __init__(self, top_n: int = 10, per_issuer: int = 1, max_duration: float = 3.0, min_spread_bp: float = 0.0,
                 ofz_min_share: float = 0.0, rank: str = "spread", min_excess_bp: float = 0.0, min_peers: int = 5,
                 same_sector: bool = False, dur_window: float = 1.0, min_gap: int = 0):
        if rank not in RANKS:
            raise ValueError(f"rank={rank}: допустимо {', '.join(RANKS)}")
        self.top_n, self.per_issuer, self.max_duration = top_n, per_issuer, max_duration
        self.min_spread_bp, self.ofz_min_share = min_spread_bp, ofz_min_share
        self.rank, self.min_excess_bp, self.min_peers = rank, min_excess_bp, min_peers
        self.same_sector, self.dur_window = same_sector, (dur_window if dur_window and dur_window > 0 else None)
        self.min_gap = int(min_gap)
        self._reasons: dict[str, str] = {}
        self.excess: dict[str, float] = {}     # секид -> превышение над ориентиром выбранной методики (б.п.)
        self.peers: dict = {}                  # секид -> PeerStats (считается всегда — для обоснования)

    def _excess(self, universe: list, ctx: MarketContext) -> dict[str, float]:
        """Превышение спреда над ориентиром выбранной методики; бумаги без ориентира в словарь не попадают."""
        from ..analytics.peers import peer_table
        self.peers = peer_table(universe, min_peers=self.min_peers, same_sector=self.same_sector, dur_window=self.dur_window)
        if self.rank == "model":
            from ..analytics.fair_spread import features_of, fit_fair_spread
            model = fit_fair_spread(universe)
            return {r.secid: (model.residual(features_of(r), r.metrics.g_spread) if model.ok else r.metrics.g_spread) for r in universe}
        if self.rank == "peers":
            return {s: ps.excess for s, ps in self.peers.items()}
        if self.rank == "quality":
            return {s: ps.excess for s, ps in self.peers.items()
                    if s in ctx.quality_stats and ctx.quality_stats[s].gap is not None and ctx.quality_stats[s].gap >= self.min_gap}
        if self.rank == "history":
            return {r.secid: ctx.history_stats[r.secid].chg30 for r in universe
                    if r.secid in ctx.history_stats and ctx.history_stats[r.secid].chg30 is not None and ctx.history_stats[r.secid].reliable}
        if self.rank == "issuer":
            return {r.secid: ctx.issuer_stats[r.secid].resid for r in universe if r.secid in ctx.issuer_stats}
        return {r.secid: r.metrics.g_spread for r in universe}

    def ranked(self, ctx: MarketContext) -> list:
        # ориентир (медиана ступени / модель) считается по всему корпоративному срезу, а не только по коротким бумагам
        universe = [r for r in ctx.rows if not r.bond.is_ofz and not r.bond.is_floater and r.metrics.g_spread is not None]
        self.excess = self._excess(universe, ctx)
        rows = [r for r in universe if r.secid in self.excess and r.metrics.macaulay_duration <= self.max_duration
                and r.metrics.g_spread >= self.min_spread_bp and (self.rank == "spread" or self.excess[r.secid] >= self.min_excess_bp)]
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

    def reason(self, r, ctx: MarketContext) -> str:
        m = r.metrics
        base = (f"G-спред {m.g_spread:+.0f} б.п. (YTW {m.yield_worst:.1f}% при ОФЗ {m.yield_worst - m.g_spread / 100:.1f}% "
                f"на дюрации {m.macaulay_duration:.1f}), рейтинг {r.rating_str}")
        ps = self.peers.get(r.secid)
        if ps is not None and ps.n:
            base += f"; пиры: {ps.excess:+.0f} б.п. к медиане {ps.median:.0f} ({ps.group}, n={ps.n}), дороже {ps.pct_rank:.0%} похожих"
        if self.rank == "model":
            base += f"; модель: {self.excess[r.secid]:+.0f} б.п. к справедливому"
        hs = ctx.history_stats.get(r.secid)
        base += f"; история: {hs.describe()}" if hs is not None else "; история: нет данных"
        ist = ctx.issuer_stats.get(r.secid)
        if ist is not None:
            base += f"; эмитент: {ist.describe()}"
        qs = ctx.quality_stats.get(r.secid)
        base += f"; метрики: {qs.describe()}" if qs is not None else "; метрики: нет в книге"
        return base

    def targets(self, ctx: MarketContext) -> dict[str, float]:
        self._reasons = {}
        picks = self.ranked(ctx)
        ofz = sorted((r for r in ctx.rows if r.bond.is_ofz and not r.bond.is_floater and not r.bond.is_linker),
                     key=lambda r: abs(r.metrics.macaulay_duration - 1.0))
        w: dict[str, float] = {}
        corp_share = 1.0 - (self.ofz_min_share if ofz and self.ofz_min_share > 0 else 0.0)
        for r in picks:
            w[r.secid] = corp_share / len(picks)
            self._reasons[r.secid] = self.reason(r, ctx)
        if ofz and self.ofz_min_share > 0:
            w[ofz[0].secid] = self.ofz_min_share if picks else 1.0
            self._reasons[ofz[0].secid] = "ликвидное ядро в ОФЗ"
        return w

    def explain(self, ctx: MarketContext) -> dict[str, str]:
        return dict(self._reasons)
