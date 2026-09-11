"""Спред бумаги против её собственной истории: «всегда так торговалась» или «что-то случилось».

Срез на одну дату не отличает цену риска эмитента от события. История G-спреда за окно (обычно 60–90 дней)
даёт: медиану и робастный разброс (MAD), z-оценку текущего спреда, изменение за 30/60 дней, положение внутри
диапазона и дату первой сделки в окне (первичка — бумага разместилась недавно, истории почти нет).

Здесь только математика над готовыми точками (дата, спред б.п.); загрузка с MOEX — в data/history.py.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

MIN_POINTS = 10          # меньше — статистика бессмысленна, считаем «нет истории»
FRESH_POINTS = 30        # первичка: сделок в окне меньше и первая сделка заметно позже начала окна
Z_CAP = 5.0


@dataclass
class SpreadStats:
    n: int
    first: date                   # первая сделка в окне
    last: date                    # последняя сделка в окне
    now: float                    # текущий спред, б.п. (в той же методике, что история)
    median: float
    mad: float                    # робастный масштаб (1.4826·MAD), б.п.
    z: float                      # (now − median) / mad, ограничено ±Z_CAP
    lo: float
    hi: float
    pct_rank: float               # доля наблюдений ниже текущего (1.0 — шире, чем когда-либо в окне)
    chg30: Optional[float]        # now − медиана спредов 25–35 дней назад, б.п.
    chg60: Optional[float]        # now − медиана спредов 55–65 дней назад, б.п.
    fresh: bool                   # похоже на первичку: мало наблюдений и первая сделка позже начала окна
    window_days: int
    note: str = ""                # почему истории нельзя верить (напр. «оферта 2026-09-25»): доходность к близкой
                                  # оферте гиперчувствительна к цене, ряд и динамика в б.п. теряют смысл

    @property
    def reliable(self) -> bool:
        return not self.note

    @property
    def regime(self) -> str:
        """Одно слово для колонки: расширение / сжатие / стабильно / первичка / оферта."""
        if self.note:
            return "оферта"
        if self.fresh:
            return "первичка"
        if self.chg30 is not None and self.chg30 >= 150 and self.z >= 1.5:
            return "расширение"
        if self.chg30 is not None and self.chg30 <= -150 and self.z <= -1.5:
            return "сжатие"
        return "стабильно"

    def describe(self) -> str:
        parts = [f"спред {self.now:.0f} при медиане {self.median:.0f} за {self.window_days} дн. (z={self.z:+.1f}, диапазон {self.lo:.0f}–{self.hi:.0f})"]
        if self.chg30 is not None:
            parts.append(f"за 30 дн. {self.chg30:+.0f} б.п.")
        if self.chg60 is not None:
            parts.append(f"за 60 дн. {self.chg60:+.0f} б.п.")
        if self.fresh:
            parts.append(f"первая сделка {self.first}, наблюдений {self.n} — первичка")
        if self.note:
            parts.append(f"ненадёжно: {self.note}")
        return "; ".join(parts)


def _median_near(points: list[tuple[date, float]], settle: date, days_ago: int, halfwidth: int = 5) -> Optional[float]:
    lo, hi = settle - timedelta(days=days_ago + halfwidth), settle - timedelta(days=days_ago - halfwidth)
    vals = [s for d, s in points if lo <= d <= hi]
    return statistics.median(vals) if vals else None


def spread_stats(points: list[tuple[date, float]], now: Optional[float], settle: date, window_days: int = 90) -> Optional[SpreadStats]:
    """points — (дата, G-спред б.п.) за окно; now — текущий спред в той же методике (None → последняя точка)."""
    pts = sorted((d, float(s)) for d, s in points if d is not None and s is not None and d <= settle)
    if len(pts) < MIN_POINTS:
        return None
    if now is None:
        now = pts[-1][1]
    spreads = [s for _, s in pts]
    med = statistics.median(spreads)
    mad = statistics.median(abs(s - med) for s in spreads) * 1.4826
    if mad < 1e-9:
        sd = statistics.pstdev(spreads)
        mad = sd if sd > 1e-9 else 0.0
    if mad > 0:
        z = max(-Z_CAP, min(Z_CAP, (now - med) / mad))
    else:   # ряд без разброса: любой сдвиг от него — предельная аномалия
        z = 0.0 if abs(now - med) < 1e-9 else (Z_CAP if now > med else -Z_CAP)
    below = sum(1 for s in spreads if s < now)
    past30 = _median_near(pts, settle, 30)
    past60 = _median_near(pts, settle, 60)
    window_start = settle - timedelta(days=window_days)
    fresh = len(pts) < FRESH_POINTS and pts[0][0] > window_start + timedelta(days=7)
    return SpreadStats(
        n=len(pts), first=pts[0][0], last=pts[-1][0], now=now, median=med, mad=mad, z=z,
        lo=min(spreads), hi=max(spreads), pct_rank=below / len(spreads),
        chg30=(now - past30) if past30 is not None else None,
        chg60=(now - past60) if past60 is not None else None,
        fresh=fresh, window_days=window_days,
    )
