from __future__ import annotations

from app.strategies.breakout import BreakoutStrategy
from app.strategies.mean_reversion import MeanReversionStrategy
from app.strategies.opening_range_breakout import OpeningRangeBreakoutStrategy
from app.strategies.strategy import Strategy
from app.strategies.trend import TrendStrategy

# Maps BotConfig.strategy_class (a plain string, since Strategy itself is
# never persisted) back to the actual class so the daemon can construct it.
STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "TrendStrategy": TrendStrategy,
    "BreakoutStrategy": BreakoutStrategy,
    "MeanReversionStrategy": MeanReversionStrategy,
    "OpeningRangeBreakoutStrategy": OpeningRangeBreakoutStrategy,
}


def get_strategy_class(name: str) -> type[Strategy]:
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown strategy_class {name!r}; known strategies: {sorted(STRATEGY_REGISTRY)}"
        ) from None
