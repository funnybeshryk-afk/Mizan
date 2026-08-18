from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.database.models import SignalAction
from app.market.provider import Bar


@dataclass(frozen=True)
class Signal:
    """A strategy's momentary opinion - not an order, not money, just a
    classification of what the data currently looks like."""

    action: SignalAction
    price: Decimal | None = None


class Strategy(abc.ABC):
    """Pure data -> signal decision. A Strategy never touches Portfolio,
    Broker, or money/risk limits, and never calls submit_order() - it only
    looks at bars and returns a Signal. Execution (if any) is a separate
    concern handled elsewhere.

    self.params holds the strategy's configuration (periods, thresholds,
    ...) so a specific run's parameters can be recorded verbatim for
    reproducible backtests later.
    """

    def __init__(self, params: dict[str, Any] | None = None):
        self.params: dict[str, Any] = dict(params or {})

    @abc.abstractmethod
    def generate_signal(self, bars: list[Bar]) -> Signal:
        ...
