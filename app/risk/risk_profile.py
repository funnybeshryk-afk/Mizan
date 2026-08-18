from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskProfile:
    """A named bundle of position-sizing and loss-limit rules. Not a DB
    table - like Bar and Signal, it's a plain value object; a strategy
    doesn't own or reference one, it's supplied to a RiskManager alongside
    a strategy's decisions at execution time.
    """

    name: str
    max_position_pct: float
    max_daily_loss_pct: float
    max_drawdown_pct: float
    stop_loss_required: bool
    stop_loss_pct: float
    take_profit_pct: float
    max_open_positions: int
    max_leverage: float
    # NOT ENFORCED ANYWHERE (Stage 10 audit, 2026): RiskManager.evaluate()
    # sizes purely from the caller's own PortfolioState.current_equity via
    # max_position_pct - it never reads this field. Each BotConfig has its
    # own dedicated Account (app/automation/setup_bots.py), so two bots
    # sharing a risk_profile_name do not split a pool - each gets its own
    # full, independent capital. Treat this value as aspirational
    # documentation only, not a guarantee, until/unless a future change
    # actually wires it into sizing across bots sharing a profile.
    capital_allocation_pct: float


# Diversify-and-protect: many small positions, tight stop, low loss
# tolerance - so a larger share of total capital is safe to allocate to it.
CONSERVATIVE = RiskProfile(
    name="CONSERVATIVE",
    max_position_pct=0.05,
    max_daily_loss_pct=0.01,
    max_drawdown_pct=0.09,
    stop_loss_required=True,
    stop_loss_pct=0.025,
    take_profit_pct=0.05,
    max_open_positions=5,
    max_leverage=1.0,
    capital_allocation_pct=0.6,
)

# Concentrate-and-contain: fewer, larger positions with a wider stop and
# higher loss tolerance per trade - so only a smaller slice of total
# capital is put at risk through this profile at all.
AGGRESSIVE = RiskProfile(
    name="AGGRESSIVE",
    max_position_pct=0.18,
    max_daily_loss_pct=0.04,
    max_drawdown_pct=0.15,
    stop_loss_required=True,
    stop_loss_pct=0.06,
    take_profit_pct=0.12,
    max_open_positions=3,
    max_leverage=1.0,
    capital_allocation_pct=0.25,
)

# Maps BotConfig.risk_profile_name (a plain string, since RiskProfile itself
# is never persisted) back to the actual value object.
RISK_PROFILES: dict[str, RiskProfile] = {
    "CONSERVATIVE": CONSERVATIVE,
    "AGGRESSIVE": AGGRESSIVE,
}


def get_risk_profile(name: str) -> RiskProfile:
    try:
        return RISK_PROFILES[name]
    except KeyError:
        raise ValueError(
            f"Unknown risk profile {name!r}; known profiles: {sorted(RISK_PROFILES)}"
        ) from None
