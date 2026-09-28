"""Cost budgets for experimental small probes, never a return forecast."""

from decimal import Decimal

from crypto_agent.config import validate_intraday_policy
from crypto_agent.models import AgentError, RiskResult, decimal

BPS = Decimal(10000)


def round_trip_cost_bps(market, risk) -> Decimal:
    bid, ask = decimal(market.bid), decimal(market.ask)
    if not 0 < bid <= ask:
        raise AgentError("Invalid quote for intraday cost budget")
    fee, slip = decimal(risk.get("fee_buffer_bps")), decimal(risk.get("slippage_bps"))
    if not 0 < fee < 1000 or not 0 < slip < 1000:
        raise AgentError("Invalid intraday cost buffers")
    return (ask - bid) / ((ask + bid) / 2) * BPS + 2 * (fee + slip)


def probe_position_cap(strategy, market, risk) -> Decimal:
    if validate_intraday_policy(strategy) != "capped_probe":
        raise AgentError("Probe requires capped_probe policy")
    return min(
        decimal(strategy["intraday_probe_max_position_usd"]),
        decimal(strategy["intraday_probe_cost_budget_usd"]) * BPS / round_trip_cost_bps(market, risk),
    )


def validate_intraday_order(order, market, portfolio, strategy, risk) -> RiskResult:
    """Recheck total held + proposed exposure with the execution quote and saved limit."""
    if strategy.get("name") != "intraday_ai":
        return RiskResult(True, ())
    try:
        policy = validate_intraday_policy(strategy)
        if order.side == "buy" and policy == "capped_probe":
            value = (portfolio.quantity_for(order.symbol) + order.quantity) * max(
                order.limit_price, market.ask
            )
            if value > probe_position_cap(strategy, market, risk):
                raise AgentError("Intraday probe exceeds total position or round-trip cost budget")
        return RiskResult(True, ())
    except (AgentError, TypeError, ValueError, ArithmeticError) as exc:
        return RiskResult(False, (str(exc) if isinstance(exc, AgentError) else "Invalid probe budget",))
