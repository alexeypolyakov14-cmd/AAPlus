from .base import MarketContext, Signal, Strategy, Side
from .ladder import LadderStrategy
from .spread import SpreadMeanReversionStrategy
from .rate_cycle import RateCycleStrategy
from .carry import CarryRollDownStrategy

STRATEGIES = {
    "ladder": LadderStrategy,
    "spread": SpreadMeanReversionStrategy,
    "rate_cycle": RateCycleStrategy,
    "carry": CarryRollDownStrategy,
}


def make_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError(f"Неизвестная стратегия '{name}'. Доступные: {', '.join(STRATEGIES)}")
    return STRATEGIES[name](**(params or {}))


__all__ = [
    "MarketContext", "Signal", "Strategy", "Side", "STRATEGIES", "make_strategy",
    "LadderStrategy", "SpreadMeanReversionStrategy", "RateCycleStrategy", "CarryRollDownStrategy",
]
