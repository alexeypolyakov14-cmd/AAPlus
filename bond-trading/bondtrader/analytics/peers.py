"""Группа пиров: с кем сравнивать спред бумаги, чтобы понять, «платят ли за неё больше, чем за похожих».

Пиры = корпоративные бумаги той же ступени рейтинга (без рейтинга — своя группа), опционально того же сектора
и близкой дюрации. Если пиров меньше min_peers, группа расширяется каскадом: убираем сектор → расширяем
рейтинг на ±1 ступень → убираем окно дюрации → ±2 ступени → все корпораты. Для бумаг без рейтинга сектор
учитывается всегда и первым (лизинг сравниваем с лизингом), затем все безрейтинговые. Так у каждой бумаги есть
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
    trimmed: int = 0                # сколько стрессовых пиров исключено из ориентира (спред кратно выше группы)


STRESS_GAP = 1.4                    # разрыв между соседними спредами (кратно), по которому группа делится на «здоровых» и «стрессовых»
STRESS_MULT = 1.8                   # верхняя часть считается стрессовой, если её медиана выше медианы нижней в STRESS_MULT раз


def trim_stressed(items: list, min_keep: int = 3, gap: float = STRESS_GAP, mult: float = STRESS_MULT, rounds: int = 2,
                  min_core: int = 4) -> list[float]:
    """Здоровая часть группы пиров. items — спреды или пары (спред, эмитент). Сортируем, ищем самый большой кратный
    разрыв между соседями; если он ≥ gap, верхняя часть в mult раз шире нижней И нижняя часть — большинство
    эмитентов группы, верхнюю отбрасываем (до rounds раз). Считаем по эмитентам, а не по выпускам: в A- сейчас
    Самолёт и Брусника семью выпусками на 900–1850 б.п. делают медиану 528, и здоровые ИЭК/Софтлайн на 390–410
    выглядят «дешевле пиров»; по эмитентам же здоровых пять против двух. Обратный случай — BBB+, где три бумаги
    на ~270 и семь эмитентов на 600–1800: там широкий спред и есть норма ступени, нижнее меньшинство не ориентир.
    Стрессовые остаются в pct_rank и в списке пиров. Для групп без рейтинга не применяется (см. peer_stats)."""
    pairs = sorted((float(x[0]), str(x[1])) if isinstance(x, (tuple, list)) else (float(x), f"#{i}") for i, x in enumerate(items))
    for _ in range(rounds):
        if len(pairs) < min_keep + 1:
            break
        best_i, best_ratio = -1, 0.0
        for i in range(min_keep - 1, len(pairs) - 1):
            if pairs[i][0] <= 50:                  # около нуля кратность не имеет смысла (AAA, отрицательные спреды)
                continue
            ratio = pairs[i + 1][0] / pairs[i][0]
            if ratio > best_ratio:
                best_i, best_ratio = i, ratio
        if best_i < 0 or best_ratio < gap:
            break
        lower, upper = pairs[:best_i + 1], pairs[best_i + 1:]
        if statistics.median(x for x, _ in upper) < mult * statistics.median(x for x, _ in lower):
            break
        lower_issuers = {k for _, k in lower}
        # здоровая часть должна быть не только большинством эмитентов, но и сама по себе группой (≥ min_core бумаг трёх эмитентов, по умолчанию 4):
        # иначе три бумаги на 170 б.п. объявляют «стрессовой» всю ступень A+ (Система, Автобан, ВИС на 380–800)
        if len(lower_issuers) < len({k for _, k in upper}) or len(lower) < min_core or len(lower_issuers) < 3:
            break
        pairs = lower
    return [x for x, _ in pairs]


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
    if g is None:
        # без рейтинга «ступени» нет, и корзина безрейтинговых разношёрстна (суборды Сбера рядом с ВДО 3-го уровня):
        # сравниваем сначала внутри сектора, потом по всем безрейтинговым той же дюрации, потом без окна
        steps = [(0, True, dur_window), (0, False, dur_window), (0, True, None), (0, False, None)]
    else:
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
    elif g is None:
        parts.append("все сектора")
    if window is not None:
        d = r.metrics.macaulay_duration
        parts.append(f"дюрация {max(0.0, d - window):.1f}–{d + window:.1f}")
    return ", ".join(parts)


def peer_stats(r, universe: list, **kw) -> PeerStats:
    peers, label, widened = peer_group(r, universe, **kw)
    sp = sorted(x.metrics.g_spread for x in peers)
    if not sp:
        return PeerStats(label, 0, float("nan"), float("nan"), float("nan"), 0.0, 0.0, [], widened)
    core = trim_stressed([(x.metrics.g_spread, x.bond.issuer_key) for x in peers]) if _grade(r) is not None else sp
    med = statistics.median(core)
    my = r.metrics.g_spread
    below = sum(1 for s in sp if s < my)          # место среди всех пиров, включая стрессовых
    trimmed = len(sp) - len(core)
    if trimmed:
        label += f", без {trimmed} стрессовых"
    return PeerStats(label, len(core), med, core[len(core) // 4], core[(3 * len(core)) // 4], my - med, below / len(sp), peers, widened, trimmed)


def peer_table(universe: list, **kw) -> dict[str, PeerStats]:
    """Статистика пиров для всех бумаг среза (для стратегии и отчёта)."""
    return {r.secid: peer_stats(r, universe, **kw) for r in universe if r.metrics.g_spread is not None}
