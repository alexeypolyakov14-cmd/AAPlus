"""Группа пиров: с кем сравнивать спред бумаги, чтобы понять, «платят ли за неё больше, чем за похожих».

Пиры = корпоративные бумаги той же ступени рейтинга (без рейтинга — своя группа), опционально того же сектора
и близкой дюрации. Если пиров меньше min_peers, группа расширяется каскадом: убираем сектор → расширяем
рейтинг на ±1 ступень → убираем окно дюрации → ±2 ступени → все корпораты. Так у каждой бумаги есть
ориентир, а описание группы говорит, насколько он «размыт».

Результат: медиана и квартили спреда пиров, превышение над медианой (excess, б.п.), место бумаги среди пиров
(pct_rank: доля пиров с меньшим спредом) и сами пиры — для объяснения в отчёте.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional

from ..data.ratings import GRADE, SCALE

UNRATED = "—"


@dataclass
class PeerStats:
    group: str                      # человекочитаемое описание группы
    n: int
    median: float
    p25: float
    p75: float
    excess: float                   # спред бумаги − медиана пиров, б.п.
    pct_rank: float                 # доля пиров со спредом ниже (0..1); 0.9 — бумага дороже 90% похожих
    peers: list = field(default_factory=list)   # ScreenRow пиров (без самой бумаги)
    widened: int = 0                # сколько шагов расширения понадобилось (0 — точная группа)


def _grade(r) -> Optional[int]:
    return GRADE.get(r.rating.rating) if r.rating is not None else None


def _label(r) -> str:
    return r.rating.rating if r.rating is not None else UNRATED


def peer_group(r, universe: list, *, min_peers: int = 5, same_sector: bool = False, dur_window: Optional[float] = 1.0,
               max_notch: int = 2) -> tuple[list, str, int]:
    """(пиры, описание группы, число расширений). universe — корпоративные строки скрина с g_spread."""
    g = _grade(r)
    dur = r.metrics.macaulay_duration
    cands = [x for x in universe if x.secid != r.secid and x.metrics.g_spread is not None and not x.bond.is_ofz and not x.bond.is_floater]

    def select(notch: int, sector: bool, window: Optional[float]) -> list:
        out = []
        for x in cands:
            xg = _grade(x)
            if g is None:
                if xg is not None:
                    continue
            else:
                if xg is None or abs(xg - g) > notch:
                    continue
            if sector and (x.sector or "") != (r.sector or ""):
                continue
            if window is not None and abs(x.metrics.macaulay_duration - dur) > window:
                continue
            out.append(x)
        return out

    # каскад расширения: от точной группы к самой широкой
    steps = [(0, same_sector, dur_window), (0, False, dur_window), (1, False, dur_window), (1, False, None)]
    if max_notch >= 2:
        steps.append((2, False, None))
    for i, (notch, sector, window) in enumerate(steps):
        peers = select(notch, sector, window)
        if len(peers) >= min_peers:
            return peers, _describe(r, notch, sector, window), i
    return cands, "все корпораты", len(steps)


def _describe(r, notch: int, sector: bool, window: Optional[float]) -> str:
    g = _grade(r)
    if g is None:
        rating = "без рейтинга"
    elif notch == 0:
        rating = _label(r)
    else:
        lo, hi = max(0, g - notch), min(len(SCALE) - 1, g + notch)
        rating = f"{SCALE[lo]}…{SCALE[hi]}"
    parts = [rating]
    if sector:
        parts.append(f"сектор {r.sector or 'other'}")
    if window is not None:
        d = r.metrics.macaulay_duration
        parts.append(f"дюрация {max(0.0, d - window):.1f}–{d + window:.1f}")
    return ", ".join(parts)


def peer_stats(r, universe: list, **kw) -> PeerStats:
    peers, label, widened = peer_group(r, universe, **kw)
    sp = sorted(x.metrics.g_spread for x in peers)
    if not sp:
        return PeerStats(label, 0, float("nan"), float("nan"), float("nan"), 0.0, 0.0, [], widened)
    med = statistics.median(sp)
    my = r.metrics.g_spread
    below = sum(1 for s in sp if s < my)
    return PeerStats(label, len(sp), med, sp[len(sp) // 4], sp[(3 * len(sp)) // 4], my - med, below / len(sp), peers, widened)


def peer_table(universe: list, **kw) -> dict[str, PeerStats]:
    """Статистика пиров для всех бумаг среза (для стратегии и отчёта)."""
    return {r.secid: peer_stats(r, universe, **kw) for r in universe if r.metrics.g_spread is not None}
