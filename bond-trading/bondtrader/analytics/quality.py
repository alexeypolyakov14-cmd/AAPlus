"""Вторая ось отбора: разрыв между выданным рейтингом и тем, что говорят метрики релиза агентства.

Ценовое ранжирование (премия к пирам) ищет, за что платят больше. Эта ось отвечает, заслуженно ли:
gap ≥ 0 — метрики не хуже ступени, премия может быть неэффективностью; gap < 0 — метрики хуже ступени,
премия — плата за риск, который рейтинг не отразил. Без чисел (финансовый сектор, НКР/АКРА без метрик) — None,
бумага идёт в ручную проверку, а не получает выдуманную оценку.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..data.metrics import AgencyMetrics, MetricsBook, implied_grade, rating_gap


@dataclass
class QualityStats:
    key: str
    actual: Optional[str]          # выданный рейтинг (из книги рейтингов)
    implied: Optional[str]         # ступень по метрикам
    gap: Optional[int]             # grade(actual) − grade(implied): < 0 метрики хуже рейтинга
    metrics: AgencyMetrics

    @property
    def verdict(self) -> str:
        if self.gap is None:
            return "нет оценки"
        if self.gap > 0:
            return "метрики лучше рейтинга"
        if self.gap == 0:
            return "метрики соответствуют рейтингу"
        return "метрики хуже рейтинга"

    def describe(self) -> str:
        s = self.metrics.describe()
        if self.implied:
            s += f" → ступень по метрикам {self.implied}"
            if self.gap is not None:
                s += f", разрыв {self.gap:+d} ({self.verdict})"
        return s


def quality_stats(rows, book: Optional[MetricsBook]) -> dict[str, QualityStats]:
    """secid -> QualityStats по бумагам, чей эмитент есть в книге метрик."""
    out: dict[str, QualityStats] = {}
    if book is None or not len(book):
        return out
    cache: dict[str, Optional[QualityStats]] = {}
    for r in rows:
        if r.bond.is_ofz:
            continue
        key = r.bond.issuer_key
        actual = r.rating.rating if r.rating else None
        ck = f"{key}|{actual}"
        if ck not in cache:
            m = book.lookup(key)
            if m is None:
                cache[ck] = None
            else:
                if not m.sector and r.sector:
                    m.sector = r.sector
                imp = implied_grade(m)
                cache[ck] = QualityStats(key, actual, imp, rating_gap(actual, imp), m)
        q = cache[ck]
        if q is not None:
            out[r.secid] = q
    return out
