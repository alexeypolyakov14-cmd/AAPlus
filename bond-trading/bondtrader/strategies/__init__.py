from .base import MarketContext, Signal, Strategy, Side
from .ladder import LadderStrategy
from .spread import SpreadMeanReversionStrategy
from .rate_cycle import RateCycleStrategy
from .carry import CarryRollDownStrategy
from .carry_spread import CarrySpreadStrategy
from .value_hy import ValueHYStrategy

STRATEGIES = {
    "ladder": LadderStrategy,
    "spread": SpreadMeanReversionStrategy,
    "rate_cycle": RateCycleStrategy,
    "carry": CarryRollDownStrategy,
    "carry_spread": CarrySpreadStrategy,
    "value_hy": ValueHYStrategy,
}


def make_strategy(name: str, params: dict | None = None) -> Strategy:
    import inspect
    import logging
    if name not in STRATEGIES:
        raise KeyError(f"Неизвестная стратегия '{name}'. Доступные: {', '.join(STRATEGIES)}")
    cls = STRATEGIES[name]
    allowed = set(inspect.signature(cls.__init__).parameters) - {"self"}
    params = dict(params or {})
    unknown = sorted(set(params) - allowed)
    if unknown:
        logging.getLogger(__name__).warning("%s: параметры %s не поддерживаются и проигнорированы", name, ", ".join(unknown))
    return cls(**{k: v for k, v in params.items() if k in allowed})


__all__ = [
    "MarketContext", "Signal", "Strategy", "Side", "STRATEGIES", "make_strategy",
    "LadderStrategy", "SpreadMeanReversionStrategy", "RateCycleStrategy", "CarryRollDownStrategy", "CarrySpreadStrategy", "ValueHYStrategy",
]
