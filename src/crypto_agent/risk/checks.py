"""Independent, fail-closed validation of data, decisions and every order."""

from datetime import datetime
from decimal import Decimal

from crypto_agent.config import validate_risk
from crypto_agent.models import (
    AgentError,
    AssetRules,
    MarketSnapshot,
    OrderIntent,
    PortfolioSnapshot,
    RiskResult,
    TradeDecision,
    decimal,
    timestamp,
    utcnow,
)

BPS = Decimal(10000)
FUTURE_SKEW_SECONDS = Decimal(5)


def _bounds(limits, symbol):
    value = limits["price_bounds_usd"][symbol]
    return max(value["min"], limits["min_price_usd"]), min(value["max"], limits["max_price_usd"])


def _age(value: datetime, now: datetime) -> Decimal:
    return decimal((now - timestamp(value)).total_seconds(), "timestamp age")


def _validate_inputs(decision, market, portfolio, asset, risk, daily_start_equity, now, market_prices=None):
    if not isinstance(risk, dict):
        raise AgentError("Missing required risk limits")
    limits = validate_risk(risk)
    now = timestamp(now or utcnow())
    if market.symbol not in limits["allowed_symbols"] or asset.symbol != market.symbol:
        raise AgentError("Market and asset must match the configured whitelist")
    if asset.tradable is not True or portfolio.tradable is not True:
        raise AgentError("Asset or account is not tradable")
    for name in ("min_order_size", "quantity_increment", "price_increment"):
        if decimal(getattr(asset, name), name) <= 0:
            raise AgentError(f"Asset {name} must be positive")
    prices = [decimal(getattr(market, name), name) for name in ("price", "bid", "ask")]
    price, bid, ask = prices
    minimum, maximum = _bounds(limits, market.symbol)
    if any(not minimum <= value <= maximum for value in prices):
        raise AgentError("Market price is outside configured sanity bounds")
    if bid > ask or not bid <= price <= ask:
        raise AgentError("Crossed or inconsistent market quote")
    if (ask - bid) / ((ask + bid) / 2) * BPS > limits["max_spread_bps"]:
        raise AgentError("Market spread exceeds limit")
    for label, snapshot in (("Market", market), ("Account", portfolio)):
        age = _age(snapshot.observed_at, now)
        if age < -FUTURE_SKEW_SECONDS:
            raise AgentError(f"{label} timestamp is in the future")
        if age > limits["max_data_age_seconds"]:
            raise AgentError(f"{label} data is stale")
    cash = decimal(portfolio.cash_usd, "cash")
    equity = decimal(portfolio.equity_usd, "equity")
    buying_power = decimal(portfolio.buying_power_usd, "non-margin buying power")
    if cash < 0 or equity <= 0 or buying_power < 0:
        raise AgentError("Account requires nonnegative cash/buying power and positive equity")
    start = decimal(daily_start_equity, "UTC daily starting equity")
    if start <= 0:
        raise AgentError("Missing positive UTC daily starting equity")
    if max(Decimal(0), start - equity) >= limits["max_daily_loss_usd"]:
        raise AgentError("UTC daily equity loss limit reached; all orders blocked")
    prices_by_symbol = {market.symbol: price}
    for symbol, value in (market_prices or {}).items():
        symbol = str(symbol)
        if symbol not in limits["allowed_symbols"]:
            raise AgentError("Market price map contains a non-whitelisted symbol")
        value = decimal(value, f"{symbol} market price")
        minimum, maximum = _bounds(limits, symbol)
        if not minimum <= value <= maximum:
            raise AgentError("Portfolio market price is outside configured sanity bounds")
        prices_by_symbol[symbol] = value
    quantity = Decimal(0)
    total_value = Decimal(0)
    for position in portfolio.positions:
        if position.symbol not in limits["allowed_symbols"]:
            raise AgentError("Portfolio contains a non-whitelisted position")
        held = decimal(position.quantity, "position quantity")
        available = decimal(position.available_quantity, "available quantity")
        entry = decimal(position.average_entry_price, "average entry price")
        if held < 0 or not 0 <= available <= held or (held > 0 and entry <= 0):
            raise AgentError("Invalid position or short position")
        if position.symbol not in prices_by_symbol:
            raise AgentError("Missing current price for a whitelisted portfolio position")
        total_value += held * prices_by_symbol[position.symbol]
        if position.symbol == market.symbol:
            quantity += held
    pending_buy_quantity = Decimal(0)
    pending_total_value = Decimal(0)
    for pending in portfolio.open_orders:
        if pending.symbol not in limits["allowed_symbols"] or pending.side not in {"buy", "sell"}:
            raise AgentError("Invalid or non-whitelisted pending order")
        requested = decimal(pending.quantity, "pending quantity")
        filled = decimal(pending.filled_quantity, "pending filled quantity")
        if requested <= 0 or not 0 <= filled <= requested:
            raise AgentError("Invalid pending order quantities")
        if pending.limit_price is not None and decimal(pending.limit_price, "pending limit") <= 0:
            raise AgentError("Invalid pending limit price")
        if pending.side == "buy":
            remainder = requested - filled
            pending_price = (
                decimal(pending.limit_price, "pending limit")
                if pending.limit_price is not None
                else prices_by_symbol.get(pending.symbol)
            )
            if pending_price is None:
                raise AgentError("Missing current price for a pending buy")
            pending_total_value += remainder * pending_price
            if pending.symbol == market.symbol:
                pending_buy_quantity += remainder
    current_pct = quantity * price / equity
    committed_pct = (quantity + pending_buy_quantity) * price / equity
    total_pct = total_value / equity
    total_committed_pct = (total_value + pending_total_value) / equity
    if total_pct > limits["max_total_position_pct"]:
        if decision is not None and (
            decision.symbol != market.symbol or decision.target_position_pct is None
        ):
            raise AgentError("Portfolio exceeds aggregate position limit")
    target = None
    if decision is not None:
        if decision.symbol != market.symbol:
            raise AgentError("Decision symbol does not match the selected market")
        hold = decision.rating == "Hold" and decision.actionable is False
        if (decision.actionable is not True and not hold) or decision.rating not in {
            "Buy",
            "Overweight",
            "Hold",
            "Underweight",
            "Sell",
            "REBALANCE",
        }:
            raise AgentError("Decision is not actionable or requires REVIEW")
        if not isinstance(decision.reason, str) or not decision.reason.strip():
            raise AgentError("Decision lacks a reason")
        if (
            not isinstance(decision.evidence, (tuple, list))
            or not decision.evidence
            or any(not isinstance(item, str) or not item.strip() for item in decision.evidence)
        ):
            raise AgentError("Decision lacks required evidence")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (decision.strategy_version, decision.model)
        ):
            raise AgentError("Decision lacks strategy/model provenance")
        target = decimal(decision.target_position_pct, "decision target position")
        maximum_target = Decimal(1) if hold else limits["max_position_pct"]
        if not 0 <= target <= maximum_target:
            raise AgentError("Decision target exceeds position limit")
        created, expires = timestamp(decision.created_at), timestamp(decision.expires_at)
        age = _age(created, now)
        ttl = decimal((expires - created).total_seconds(), "decision TTL")
        if age < -FUTURE_SKEW_SECONDS:
            raise AgentError("Decision timestamp is in the future")
        if age > limits["max_decision_age_seconds"] or now >= expires:
            raise AgentError("Decision is expired")
        if ttl <= 0 or ttl > limits["max_decision_age_seconds"]:
            raise AgentError("Decision validity exceeds configured maximum")
    if (
        decision is not None
        and decision.actionable is True
        and committed_pct > limits["max_position_pct"]
        and target >= current_pct
    ):
        raise AgentError("Current position exceeds limit; only exposure-reducing decisions allowed")
    if (
        decision is not None
        and decision.actionable is True
        and total_committed_pct > limits["max_total_position_pct"]
        and decimal(decision.target_position_pct) >= current_pct
    ):
        raise AgentError("Aggregate portfolio limit exceeded; only exposure-reducing decisions allowed")
    return limits


def validate_inputs(
    decision: TradeDecision | None,
    market: MarketSnapshot,
    portfolio: PortfolioSnapshot,
    asset: AssetRules,
    risk: dict,
    daily_start_equity: Decimal,
    now: datetime | None = None,
    market_prices: dict[str, Decimal] | None = None,
) -> RiskResult:
    """Reject missing limits, bad input or drawdown before planning any order."""
    try:
        _validate_inputs(decision, market, portfolio, asset, risk, daily_start_equity, now, market_prices)
        return RiskResult(True, ())
    except AgentError as exc:
        return RiskResult(False, (str(exc),))
    except (AttributeError, TypeError, ValueError, ArithmeticError):
        return RiskResult(False, ("Malformed input; risk validation failed closed",))


def validate_order(
    order: OrderIntent,
    decision: TradeDecision,
    market: MarketSnapshot,
    portfolio: PortfolioSnapshot,
    asset: AssetRules,
    risk: dict,
    daily_start_equity: Decimal,
    now: datetime | None = None,
    market_prices: dict[str, Decimal] | None = None,
) -> RiskResult:
    """Recompute notional, fees, precision, buying power and worst-price exposure.

    Buy exposure values all BTC at the order limit; equity is reduced by the
    configured fee reserve. A sell may reduce an existing position above its
    limit. The daily loss stop still blocks both sides. Pending orders must be
    reconciled/cancelled before an additional order can be submitted.
    """
    try:
        limits = _validate_inputs(
            decision, market, portfolio, asset, risk, daily_start_equity, now, market_prices
        )
        if decision is None or decision.actionable is not True or decision.rating == "Hold":
            raise AgentError("An actionable decision is required for every order")
        if order.symbol != market.symbol or order.side not in {"buy", "sell"}:
            raise AgentError("Invalid order symbol or side")
        if (decision.rating in {"Buy", "Overweight"} and order.side != "buy") or (
            decision.rating in {"Underweight", "Sell"} and order.side != "sell"
        ):
            raise AgentError("Order side conflicts with decision rating")
        if (
            order.time_in_force != "gtc"
            or not isinstance(order.client_order_id, str)
            or not order.client_order_id
        ):
            raise AgentError("Order requires gtc and a persisted client_order_id")
        if portfolio.open_orders:
            raise AgentError("Reconcile/cancel pending orders before submitting")
        quantity = decimal(order.quantity, "order quantity")
        price = decimal(order.limit_price, "order limit price")
        increment = decimal(asset.quantity_increment, "quantity increment")
        tick = decimal(asset.price_increment, "price increment")
        if quantity < decimal(asset.min_order_size) or quantity <= 0 or quantity % increment != 0:
            raise AgentError("Order violates quantity minimum or precision")
        if price <= 0 or price % tick != 0:
            raise AgentError("Order violates price precision")
        minimum, maximum = _bounds(limits, order.symbol)
        if not minimum <= price <= maximum:
            raise AgentError("Order price is outside sanity bounds")
        slippage = limits["slippage_bps"] / BPS
        # The planner rounds outward by one exchange tick at most.
        if order.side == "buy" and not market.ask <= price < market.ask * (1 + slippage) + tick:
            raise AgentError("Buy limit exceeds slippage bound or is below ask")
        if order.side == "sell" and not market.bid * (1 - slippage) - tick < price <= market.bid:
            raise AgentError("Sell limit exceeds slippage bound or is above bid")
        notional = quantity * price
        fee = notional * limits["fee_buffer_bps"] / BPS
        if not limits["min_order_notional_usd"] <= notional <= limits["max_order_notional_usd"]:
            raise AgentError("Order notional violates minimum/maximum limit")
        equity_after_fee = decimal(portfolio.equity_usd) - fee
        if equity_after_fee <= 0:
            raise AgentError("Fee reserve exhausts account equity")
        held = portfolio.quantity_for(order.symbol)
        target = decimal(decision.target_position_pct)
        desired_delta = decimal(portfolio.equity_usd) * target / market.price - held
        if order.side == "buy":
            if desired_delta <= 0 or quantity > desired_delta:
                raise AgentError("Buy does not match the decision target")
            spendable = min(decimal(portfolio.cash_usd), decimal(portfolio.buying_power_usd))
            if notional + fee > spendable:
                raise AgentError("Insufficient cash or non-margin buying power including fee reserve")
            position_value = (held + quantity) * price
            exposure = position_value / equity_after_fee
            if exposure > limits["max_position_pct"]:
                raise AgentError("Projected position exceeds maximum including limit price and fees")
            position_limit = limits.get("max_position_notional_usd")
            if position_limit is not None and position_value > position_limit:
                raise AgentError("Projected position exceeds maximum USD notional")
            prices_by_symbol = {market.symbol: market.price, **(market_prices or {})}
            other_value = sum(
                (
                    decimal(item.quantity) * decimal(prices_by_symbol[item.symbol])
                    for item in portfolio.positions
                    if item.symbol != order.symbol
                ),
                Decimal(0),
            )
            total_value = other_value + position_value
            if total_value / equity_after_fee > limits["max_total_position_pct"]:
                raise AgentError("Projected aggregate portfolio exceeds maximum")
            total_limit = limits.get("max_total_position_notional_usd")
            if total_limit is not None and total_value > total_limit:
                raise AgentError("Projected aggregate portfolio exceeds maximum USD notional")
        else:
            if desired_delta >= 0 or quantity > -desired_delta:
                raise AgentError("Sell does not match the decision target")
            available = portfolio.available_for(order.symbol)
            if quantity > available or quantity > held:
                raise AgentError("Sell exceeds available holdings; short selling forbidden")
            before = held * market.price / portfolio.equity_usd
            after = (held - quantity) * market.price / equity_after_fee
            if after > limits["max_position_pct"] and after >= before:
                raise AgentError("Sell fails to reduce excess exposure")
        return RiskResult(True, ())
    except AgentError as exc:
        return RiskResult(False, (str(exc),))
    except (AttributeError, TypeError, ValueError, ArithmeticError):
        return RiskResult(False, ("Malformed order; risk validation failed closed",))
