"""Deterministic, fee-aware conversion from a target exposure to one limit order."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from crypto_agent.config import validate_risk
from crypto_agent.models import (
    AgentError,
    AssetRules,
    MarketSnapshot,
    OrderIntent,
    PortfolioSnapshot,
    TradeDecision,
    decimal,
)
from crypto_agent.risk.checks import validate_inputs


def _round(value: Decimal, increment: Decimal, rounding: str = ROUND_FLOOR) -> Decimal:
    return (value / increment).to_integral_value(rounding=rounding) * increment


def plan_orders(
    decision: TradeDecision,
    market: MarketSnapshot,
    portfolio: PortfolioSnapshot,
    asset: AssetRules,
    risk: dict,
    client_order_id: str,
    market_prices: dict[str, Decimal] | None = None,
) -> list[OrderIntent]:
    """Return no order at target/dust; block conflicting outstanding orders.

    The caller separately checks its persisted UTC daily equity baseline. This
    boundary also validates all other inputs so direct planner callers cannot
    turn malformed, expired or non-actionable decisions into orders.
    """
    check = validate_inputs(
        decision, market, portfolio, asset, risk, portfolio.equity_usd, market_prices=market_prices
    )
    if not check.allowed:
        raise AgentError("; ".join(check.reasons))
    limits = validate_risk(risk)
    if not client_order_id:
        raise AgentError("A persistent client_order_id is required")
    if decision is None:
        raise AgentError("A decision is required")
    if decision.rating == "Hold":
        return []
    target = decimal(decision.target_position_pct)
    held = portfolio.quantity_for(market.symbol)
    pending = sum(
        (
            order.remaining_quantity * (1 if order.side == "buy" else -1)
            for order in portfolio.open_orders
            if order.symbol == market.symbol
        ),
        Decimal(0),
    )
    delta = portfolio.equity_usd * target / market.price - held - pending
    quantity = _round(abs(delta), asset.quantity_increment)
    if quantity < asset.min_order_size or quantity * market.price < limits["min_order_notional_usd"]:
        return []
    if portfolio.open_orders:
        raise AgentError("Reconcile/cancel pending orders before replanning")
    # A refreshed price must not turn an increase rating into a sell (or vice versa).
    if (decision.rating in {"Buy", "Overweight"} and delta <= 0) or (
        decision.rating in {"Underweight", "Sell"} and delta >= 0
    ):
        return []
    side = "buy" if delta > 0 else "sell"
    slippage = limits["slippage_bps"] / Decimal(10000)
    fee_rate = limits["fee_buffer_bps"] / Decimal(10000)
    if side == "buy":
        price = _round(market.ask * (1 + slippage), asset.price_increment, ROUND_CEILING)
        spendable = min(portfolio.cash_usd, portfolio.buying_power_usd)
        cash_quantity = spendable / (price * (1 + fee_rate))
        # (held + q) * limit <= target * (equity - q * limit * fee_rate).
        exposure_quantity = (target * portfolio.equity_usd - held * price) / (price * (1 + target * fee_rate))
        quantity = _round(
            max(Decimal(0), min(quantity, cash_quantity, exposure_quantity)), asset.quantity_increment
        )
    else:
        price = _round(market.bid * (1 - slippage), asset.price_increment)
        available = portfolio.available_for(market.symbol)
        quantity = _round(min(quantity, held, available), asset.quantity_increment)
    if limits.get("cap_order_to_limit", False) and price > 0:
        quantity = _round(min(quantity, limits["max_order_notional_usd"] / price), asset.quantity_increment)
    notional = quantity * price
    if quantity < asset.min_order_size or notional < limits["min_order_notional_usd"]:
        return []
    # Return at most one order; any remaining target needs a fresh decision cycle.
    # Without opt-in sizing, independent risk still rejects oversized orders.
    return [
        OrderIntent(
            symbol=market.symbol,
            side=side,
            quantity=quantity,
            client_order_id=client_order_id,
            limit_price=price,
            estimated_notional_usd=notional,
            estimated_fee_usd=notional * fee_rate,
        )
    ]
