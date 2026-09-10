"""Кривая бескупонной доходности ОФЗ (G-curve MOEX) и её аппроксимации."""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Iterable, Optional


@dataclass
class ZeroCurve:
    """Кусочно-линейная кривая: тенор (лет) -> доходность (% годовых)."""
    trade_date: Optional[date] = None
    points: list[tuple[float, float]] = field(default_factory=list)
    source: str = "manual"

    def __post_init__(self):
        self.points = sorted((float(t), float(y)) for t, y in self.points if t is not None and y is not None)

    def __len__(self):
        return len(self.points)

    def yield_at(self, years: float) -> float:
        if not self.points:
            raise ValueError("пустая кривая")
        xs = [p[0] for p in self.points]
        if years <= xs[0]:
            return self.points[0][1]
        if years >= xs[-1]:
            return self.points[-1][1]
        i = bisect_left(xs, years)
        x0, y0 = self.points[i - 1]
        x1, y1 = self.points[i]
        if x1 == x0:
            return y1
        return y0 + (y1 - y0) * (years - x0) / (x1 - x0)

    def slope(self, short: float = 1.0, long: float = 10.0) -> float:
        """Наклон кривой (long − short), п.п. Отрицательный — инверсия."""
        return self.yield_at(long) - self.yield_at(short)

    def shifted(self, bp: float) -> "ZeroCurve":
        return ZeroCurve(self.trade_date, [(t, y + bp / 100) for t, y in self.points], self.source)

    @classmethod
    def from_moex_zcyc(cls, payload: dict) -> "ZeroCurve":
        """Из ответа /iss/engines/stock/zcyc.json (блок yearyields)."""
        block = payload.get("yearyields") or {}
        cols = block.get("columns") or []
        rows = block.get("data") or []
        if not cols or not rows:
            raise ValueError("zcyc: пустой блок yearyields")
        ip = cols.index("period")
        iv = cols.index("value")
        idt = cols.index("tradedate") if "tradedate" in cols else None
        td = None
        if idt is not None and rows[0][idt]:
            td = date.fromisoformat(str(rows[0][idt])[:10])
        pts = [(r[ip], r[iv]) for r in rows if r[ip] is not None and r[iv] is not None]
        return cls(td, pts, source="moex_zcyc")

    @classmethod
    def from_ofz_metrics(cls, items: Iterable[tuple[float, float]], trade_date: Optional[date] = None,
                         buckets: Optional[list[float]] = None) -> "ZeroCurve":
        """Грубая аппроксимация из пар (дюрация, YTM) ОФЗ-ПД: медиана по корзинам дюрации.

        Используется как fallback, если zcyc недоступен. Это кривая доходностей
        к погашению, а не бескупонная, но для G-спредов приближение приемлемо.
        """
        buckets = buckets or [0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20]
        by_bucket: dict[float, list[float]] = {}
        for dur, ytm in items:
            if dur is None or ytm is None or dur <= 0:
                continue
            nearest = min(buckets, key=lambda b: abs(b - dur))
            by_bucket.setdefault(nearest, []).append(ytm)
        pts = [(b, median(v)) for b, v in by_bucket.items() if v]
        if len(pts) < 2:
            raise ValueError("недостаточно ОФЗ для построения кривой")
        return cls(trade_date, pts, source="ofz_fit")
