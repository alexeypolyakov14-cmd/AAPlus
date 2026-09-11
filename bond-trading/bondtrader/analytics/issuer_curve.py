"""Кривая самого эмитента: платит ли конкретный выпуск больше, чем другие выпуски того же эмитента.

Если все выпуски эмитента торгуются на 1900 б.п. — это оценка эмитента рынком (и вопрос к эмитенту, а не к
бумаге). Если один выпуск на 500 б.п. шире соседей по эмитенту с близкой дюрацией — это неэффективность
конкретной бумаги (или у неё есть особенность: оферта, амортизация, листинг, неликвид).

Ориентир для выпуска считается по ОСТАЛЬНЫМ выпускам эмитента (leave-one-out):
  • ≥3 других выпуска и разброс дюраций ≥ 0.5 года — линейная регрессия спред ~ дюрация (наклон ограничен);
  • иначе — медиана спредов других выпусков; при одном другом выпуске — он сам.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional

MAX_SLOPE_BP_PER_YEAR = 400.0   # наклон кривой эмитента по модулю больше — регрессия на 3–4 точках врёт, берём медиану


@dataclass
class IssuerCurveStats:
    issuer: str
    n_other: int                  # других выпусков эмитента в срезе
    ref: float                    # ориентир: спред, который «должен» быть у бумаги по кривой эмитента, б.п.
    resid: float                  # спред бумаги − ориентир, б.п.; >0 — платят больше, чем за соседей по эмитенту
    issuer_median: float          # медиана спредов ВСЕХ выпусков эмитента (включая бумагу)
    lo: float
    hi: float
    method: str                   # fit | median | single
    slope: Optional[float] = None # б.п. на год дюрации (для fit)

    def describe(self) -> str:
        how = {"fit": f"кривая эмитента по {self.n_other} выпускам", "median": f"медиана {self.n_other} выпусков эмитента",
               "single": "единственный другой выпуск эмитента"}[self.method]
        s = f"{self.resid:+.0f} б.п. к ориентиру {self.ref:.0f} ({how}"
        if self.slope is not None:
            s += f", наклон {self.slope:+.0f} б.п./год"
        return s + f"); выпуски эмитента {self.lo:.0f}–{self.hi:.0f}"


def _fit(xs: list[float], ys: list[float]) -> Optional[tuple[float, float]]:
    """OLS y = a + b·x; None, если x вырожден."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-9:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def issuer_curve_stats(row, siblings: list) -> Optional[IssuerCurveStats]:
    """row — ScreenRow бумаги; siblings — другие ScreenRow того же эмитента с g_spread (без самой бумаги)."""
    others = [x for x in siblings if x.secid != row.secid and x.metrics.g_spread is not None]
    if not others or row.metrics.g_spread is None:
        return None
    sp = row.metrics.g_spread
    dur = row.metrics.macaulay_duration
    xs = [x.metrics.macaulay_duration for x in others]
    ys = [x.metrics.g_spread for x in others]
    all_sp = ys + [sp]
    method, slope, ref = "median", None, statistics.median(ys)
    if len(others) == 1:
        method = "single"
    elif len(others) >= 3 and (max(xs) - min(xs)) >= 0.5:
        fit = _fit(xs, ys)
        if fit is not None and abs(fit[1]) <= MAX_SLOPE_BP_PER_YEAR:
            a, b = fit
            # не экстраполируем далеко за дюрации соседей: за пределами диапазона держим край
            x = min(max(dur, min(xs)), max(xs))
            method, slope, ref = "fit", b, a + b * x
    return IssuerCurveStats(issuer=row.bond.issuer_key, n_other=len(others), ref=ref, resid=sp - ref,
                            issuer_median=statistics.median(all_sp), lo=min(all_sp), hi=max(all_sp), method=method, slope=slope)


def issuer_curves(rows: list) -> dict[str, IssuerCurveStats]:
    """Статистика по всем бумагам среза, у чьих эмитентов есть ≥2 выпуска с спредом."""
    by_issuer: dict[str, list] = {}
    for r in rows:
        if r.bond.is_ofz or r.bond.is_floater or r.metrics.g_spread is None:
            continue
        by_issuer.setdefault(r.bond.issuer_key, []).append(r)
    out: dict[str, IssuerCurveStats] = {}
    for issues in by_issuer.values():
        if len(issues) < 2:
            continue
        for r in issues:
            st = issuer_curve_stats(r, issues)
            if st is not None:
                out[r.secid] = st
    return out
