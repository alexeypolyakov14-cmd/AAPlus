"""Модель справедливого G-спреда по срезу рынка (cross-section).

fair_spread = b0 + b_grade·grade + b_unrated·[без рейтинга] + b_dur·дюрация + b_liq·log(оборот) + b_lvl·[уровень 3]
Оценивается МНК с небольшой ридж-регуляризацией по всем бумагам скрина (кроме ОФЗ).
Остаток = факт − fair: положительный означает, что рынок требует за бумагу больше, чем за похожие по
рейтингу/сроку/ликвидности — либо недооценка, либо рынок знает что-то, чего нет в рейтинге. Именно поэтому
остаток — только одна из компонент композита стратегии value_hy, а не самостоятельный сигнал.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..data.ratings import GRADE

FEATURES = ["const", "grade", "unrated", "duration", "log_turnover", "level3"]


@dataclass
class FairSpreadModel:
    coef: dict[str, float] = field(default_factory=dict)
    n: int = 0
    r2: float = 0.0
    resid_sd: float = 1.0

    @property
    def ok(self) -> bool:
        return self.n >= 8 and bool(self.coef)

    def fair(self, x: dict[str, float]) -> float:
        return sum(self.coef.get(k, 0.0) * x.get(k, 0.0) for k in FEATURES)

    def residual(self, x: dict[str, float], actual: float) -> float:
        return actual - self.fair(x)

    def describe(self) -> str:
        if not self.ok:
            return f"модель не оценена (n={self.n})"
        c = self.coef
        return (f"n={self.n} R²={self.r2:.2f} sd={self.resid_sd:.0f} б.п.: ступень рейтинга {c.get('grade', 0):+.0f} б.п., "
                f"без рейтинга {c.get('unrated', 0):+.0f}, год дюрации {c.get('duration', 0):+.0f}, "
                f"log оборота {c.get('log_turnover', 0):+.0f}, 3-й уровень {c.get('level3', 0):+.0f}")


def features_of(row) -> dict[str, float]:
    """Признаки бумаги из ScreenRow (без импорта screener — только duck typing)."""
    grade = GRADE.get(row.rating.rating) if row.rating is not None else None
    unrated = grade is None
    if unrated:
        grade = GRADE["BB-"]   # нейтральная ступень для нерейтингованных; отдельный dummy ловит их премию
    return {
        "const": 1.0,
        "grade": float(grade),
        "unrated": 1.0 if unrated else 0.0,
        "duration": float(row.metrics.macaulay_duration),
        "log_turnover": math.log1p(max(row.quote.turnover, 0.0) / 1e6),
        "level3": 1.0 if (row.bond.list_level or 0) >= 3 else 0.0,
    }


def fit_fair_spread(rows, ridge: float = 1e-3, max_spread: Optional[float] = None) -> FairSpreadModel:
    """Оценка по корпоративным бумагам с известным G-спредом."""
    xs, ys = [], []
    for r in rows:
        gs = r.metrics.g_spread
        if r.bond.is_ofz or gs is None or r.bond.is_floater:
            continue
        if max_spread is not None and gs > max_spread:
            continue
        f = features_of(r)
        xs.append([f[k] for k in FEATURES])
        ys.append(gs)
    m = FairSpreadModel(n=len(ys))
    if len(ys) < 8:
        return m
    X, y = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    # ридж по всем, кроме константы: стабилизирует при вырожденных признаках (например, все на одном уровне)
    lam = ridge * len(ys) * np.eye(X.shape[1])
    lam[0, 0] = 0.0
    beta = np.linalg.solve(X.T @ X + lam, X.T @ y)
    pred = X @ beta
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum()) or 1.0
    m.coef = {k: float(b) for k, b in zip(FEATURES, beta)}
    m.r2 = max(0.0, 1 - ss_res / ss_tot)
    m.resid_sd = math.sqrt(ss_res / max(len(ys) - X.shape[1], 1)) or 1.0
    return m
