"""value_hy: поиск недооценённых ВДО по трём компонентам (веса настраиваются).

  1. spread   — остаток G-спреда относительно справедливого по срезу рынка (рейтинг, дюрация, ликвидность, листинг):
                бумага даёт больше, чем «положено» похожим, → кандидат.
  2. fin      — разрыв между финансовым баллом по отчётности (0..100) и баллом, «зашитым» в рейтинг:
                отчётность лучше рейтинга → рынок/агентство ещё не переоценили → кандидат. Без отчётности — 0.
  3. ret      — доходность с поправкой на дефолт: YTW − PD(рейтинг)·LGD − издержки ликвидности (½ bid/ask + проскальзывание).
  4. news     — новостной фон за окно (балл с затуханием: иски, обыски, отзыв лицензии — минус; повышение рейтинга,
                погашения — плюс). Без новостей — 0. Сильный негатив уже отсечён скринером (news_stop_score).

Композит = w_spread·z(spread) + w_fin·z(fin) + w_ret·z(ret) + w_news·z(news); z — стандартизация по вселенной.
Жёсткие стоп-факторы (не входят в композит, а отсекают): (тех)дефолт/реструктуризация в книге событий,
покрытие процентов < min_coverage, отрицательный капитал, риск рефинансирования — если отчётность есть.
Отбор top_n, веса равные (дефолты — редкие и бинарные события, поэтому концентрация в «лучших» не оправдана),
плюс ликвидное ядро в ОФЗ ofz_min_share.
"""
from __future__ import annotations

import math
from typing import Optional

from ..analytics.fair_spread import features_of, fit_fair_spread
from ..data.ratings import GRADE, SCALE
from .base import MarketContext, Strategy

# Ориентировочная годовая вероятность дефолта по национальной шкале, % (порядок величины по статистике
# агентств за 2015–2024; уточняется по мере накопления данных).
ANNUAL_PD_PCT = {
    "AAA": 0.02, "AA+": 0.05, "AA": 0.07, "AA-": 0.1, "A+": 0.2, "A": 0.3, "A-": 0.5,
    "BBB+": 0.8, "BBB": 1.2, "BBB-": 1.8, "BB+": 2.5, "BB": 3.5, "BB-": 5.0,
    "B+": 7.0, "B": 10.0, "B-": 14.0, "CCC": 25.0, "CC": 40.0, "C": 60.0, "D": 100.0,
}
UNRATED_PD_PCT = 8.0   # без рейтинга — как B+/B: агентства такие бумаги обычно не видят не случайно

# Балл отчётности, который в среднем соответствует ступени рейтинга (для компоненты fin)
GRADE_SCORE = {g: max(20.0, 100.0 - 4.0 * GRADE[g]) for g in SCALE}


def pd_of(rating: Optional[str]) -> float:
    return ANNUAL_PD_PCT.get(rating, UNRATED_PD_PCT) if rating else UNRATED_PD_PCT


def _z(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    xs = list(values.values())
    mean = sum(xs) / len(xs)
    var = sum((v - mean) ** 2 for v in xs) / len(xs)
    sd = math.sqrt(var) if var > 1e-12 else 1.0
    return {k: (v - mean) / sd for k, v in values.items()}


class ValueHYStrategy(Strategy):
    """Недооценённые ВДО: остаток спреда + отчётность vs рейтинг + доходность за вычетом ожидаемых потерь."""
    name = "value_hy"

    def __init__(self, top_n: int = 30, w_spread: float = 0.40, w_fin: float = 0.35, w_ret: float = 0.25, w_news: float = 0.15,
                 lgd: float = 0.6, horizon: float = 1.0, slippage_bp: float = 25.0, max_duration: float = 2.5,
                 ofz_min_share: float = 0.10, min_coverage: float = 1.5, hard_stops: bool = True,
                 min_composite: float = -10.0, fin_required: bool = False):
        self.top_n = top_n
        self.w_spread, self.w_fin, self.w_ret, self.w_news = w_spread, w_fin, w_ret, w_news
        self.lgd, self.horizon, self.slippage_bp = lgd, horizon, slippage_bp
        self.max_duration, self.ofz_min_share = max_duration, ofz_min_share
        self.min_coverage, self.hard_stops, self.min_composite = min_coverage, hard_stops, min_composite
        self.fin_required = fin_required
        self._reasons: dict[str, str] = {}
        self.model = None
        self.components: dict[str, dict[str, float]] = {}

    # ---- компоненты ----
    def stop_reason(self, r) -> Optional[str]:
        if getattr(r, "stop_events", None):
            e = r.stop_events[0]
            return f"{e.kind} {e.date}: {e.title[:60]}"
        fin = getattr(r, "fin", None)
        if fin is None:
            return "нет отчётности" if self.fin_required else None
        if not self.hard_stops:
            return None
        if fin.equity <= 0:
            return "отрицательный капитал"
        if fin.interest_coverage is not None and fin.interest_coverage < self.min_coverage:
            return f"покрытие процентов {fin.interest_coverage:.1f}x < {self.min_coverage}"
        if any("рефинансирования" in f for f in fin.flags):
            return "риск рефинансирования"
        return None

    def compute(self, ctx: MarketContext) -> dict[str, dict[str, float]]:
        corp = [r for r in ctx.rows if not r.bond.is_ofz and not r.bond.is_floater and r.metrics.macaulay_duration <= self.max_duration]
        self.model = fit_fair_spread(corp)
        spread: dict[str, float] = {}
        fin: dict[str, float] = {}
        ret: dict[str, float] = {}
        news: dict[str, float] = {}
        stops: dict[str, str] = {}
        for r in corp:
            why = self.stop_reason(r)
            if why:
                stops[r.secid] = why
                continue
            m = r.metrics
            gs = m.g_spread if m.g_spread is not None else 0.0
            spread[r.secid] = self.model.residual(features_of(r), gs) if self.model.ok else 0.0
            rating = r.rating.rating if r.rating is not None else None
            f = getattr(r, "fin", None)
            fin[r.secid] = (f.score - GRADE_SCORE.get(rating, GRADE_SCORE["B"])) if f is not None else 0.0
            ba = r.quote.bid_ask_spread_pct or 0.5
            liq_cost = (ba / 2 + self.slippage_bp / 100) / self.horizon
            ret[r.secid] = m.yield_worst - pd_of(rating) * self.lgd - liq_cost
            ns = getattr(r, "news", None)
            news[r.secid] = ns.score if ns is not None else 0.0
        zs, zf, zr, zn = _z(spread), _z(fin), _z(ret), _z(news)
        out: dict[str, dict[str, float]] = {}
        for s in spread:
            comp = self.w_spread * zs[s] + self.w_fin * zf[s] + self.w_ret * zr[s] + self.w_news * zn[s]
            out[s] = {"spread_resid": spread[s], "fin_gap": fin[s], "adj_return": ret[s], "news": news[s],
                      "z_spread": zs[s], "z_fin": zf[s], "z_ret": zr[s], "z_news": zn[s], "composite": comp}
        self.stops = stops
        self.components = out
        return out

    # ---- целевые веса ----
    def targets(self, ctx: MarketContext) -> dict[str, float]:
        self._reasons = {}
        comp = self.compute(ctx)
        ranked = [s for s, c in sorted(comp.items(), key=lambda kv: -kv[1]["composite"]) if c["composite"] >= self.min_composite]
        picks = ranked[: self.top_n]
        by_id = ctx.by_id
        ofz = sorted((r for r in ctx.rows if r.bond.is_ofz and not r.bond.is_floater and not r.bond.is_linker),
                     key=lambda r: abs(r.metrics.macaulay_duration - 1.0))
        w: dict[str, float] = {}
        corp_share = 1.0 - (self.ofz_min_share if ofz else 0.0)
        if picks:
            for s in picks:
                w[s] = corp_share / len(picks)
        if ofz and self.ofz_min_share > 0:
            w[ofz[0].secid] = self.ofz_min_share if picks else 1.0
            self._reasons[ofz[0].secid] = "ликвидное ядро в ОФЗ"
        for s in picks:
            c = comp[s]
            r = by_id[s]
            fin_txt = f"балл {r.fin.score:.0f}" if getattr(r, "fin", None) is not None else "нет отчётности"
            self._reasons[s] = (f"композит {c['composite']:+.2f}: спред {c['spread_resid']:+.0f} б.п. к справедливому (z {c['z_spread']:+.1f}), "
                                f"{fin_txt} vs рейтинг {r.rating.rating if r.rating else '—'} (z {c['z_fin']:+.1f}), "
                                f"YTW−PD·LGD−ликв. {c['adj_return']:.1f}% (z {c['z_ret']:+.1f})"
                                + (f", новости {c['news']:+.1f} (z {c['z_news']:+.1f})" if getattr(r, "news", None) is not None and r.news.n else ""))
        return w

    def explain(self, ctx: MarketContext) -> dict[str, str]:
        return dict(self._reasons)
