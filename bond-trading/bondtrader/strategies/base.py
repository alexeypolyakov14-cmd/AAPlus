"""Интерфейс стратегии и рыночный контекст."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional

from ..analytics.curve import ZeroCurve
from ..data.cbr import KeyRateView
from ..portfolio import Portfolio
from ..screener import ScreenRow


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    secid: str
    side: Side
    target_weight: float          # целевая доля от NAV (0 для SELL)
    reason: str = ""
    strategy: str = ""
    score: float = 0.0


@dataclass
class MarketContext:
    settle: date
    rows: list[ScreenRow]                          # инвестиционная вселенная после скринера
    curve: Optional[ZeroCurve] = None
    keyrate: Optional[KeyRateView] = None
    portfolio: Portfolio = field(default_factory=Portfolio)
    spread_history: dict[str, list[float]] = field(default_factory=dict)  # secid -> история G-спредов, б.п.

    @property
    def by_id(self) -> dict[str, ScreenRow]:
        return {r.secid: r for r in self.rows}

    def held(self) -> set[str]:
        return set(self.portfolio.positions)


class Strategy:
    name = "base"

    def targets(self, ctx: MarketContext) -> dict[str, float]:
        """Целевые веса по бумагам (сумма ≤ 1). Реализуется в наследниках."""
        raise NotImplementedError

    def explain(self, ctx: MarketContext) -> dict[str, str]:
        """Пояснения к выбору по secid (для отчётов). По умолчанию пусто."""
        return {}

    def generate(self, ctx: MarketContext) -> list[Signal]:
        targets = self.targets(ctx)
        reasons = self.explain(ctx)
        signals: list[Signal] = []
        by_id = ctx.by_id
        for secid, w in sorted(targets.items(), key=lambda kv: -kv[1]):
            if w <= 0:
                continue
            side = Side.HOLD if secid in ctx.held() else Side.BUY
            score = by_id[secid].score if secid in by_id else 0.0
            signals.append(Signal(secid, side, w, reasons.get(secid, ""), self.name, score))
        for secid in ctx.held():
            if targets.get(secid, 0.0) <= 0:
                signals.append(Signal(secid, Side.SELL, 0.0, reasons.get(secid, "выход из позиции"), self.name))
        return signals


def equal_weights(ids: list[str], total: float = 1.0) -> dict[str, float]:
    if not ids:
        return {}
    w = total / len(ids)
    return {i: w for i in ids}


def duration_bucket(d: float, edges: list[float]) -> int:
    """Индекс корзины по дюрации; edges — правые границы, последняя корзина открыта."""
    for i, e in enumerate(edges):
        if d < e:
            return i
    return len(edges)
