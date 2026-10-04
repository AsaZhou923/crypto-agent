"""Cost budgets for experimental small probes, never a return forecast."""

from decimal import Decimal

from crypto_agent.config import validate_intraday_policy
from crypto_agent.data.features import build_intraday_features
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


def validate_entry_economics(order, market, decision, bars, strategy, risk) -> RiskResult:
    """Recompute the opt-in entry economics from saved bars and a fresh quote."""
    if strategy.get("intraday_require_cost_cover", False) is not True or order.side != "buy":
        return RiskResult(True, ())
    try:
        if decision.rating not in {"Buy", "Overweight"}:
            return RiskResult(True, ())
        features = build_intraday_features(
            market,
            tuple(bars),
            lookback_bars=strategy["intraday_lookback_bars"],
            min_bars=strategy["intraday_min_bars"],
            max_age_seconds=strategy["intraday_max_bar_age_seconds"],
            momentum_threshold_bps=decimal(
                strategy["intraday_momentum_threshold_bps"], "intraday momentum threshold"
            ),
            now=decision.created_at,
            expected_source="coinbase-exchange-1min-bars"
            if strategy.get("intraday_bar_source") == "coinbase_exchange"
            else None,
            max_quote_bar_deviation_bps=strategy.get("intraday_max_quote_bar_deviation_bps"),
        )
        gross_move_bps = max(
            Decimal(0),
            features.return_3m_bps,
            features.return_10m_bps,
            features.return_30m_bps,
        )
        cost_bps = round_trip_cost_bps(market, risk)
        if gross_move_bps < cost_bps:
            raise AgentError(
                "Intraday entry economics no longer cover current spread, fee and slippage buffers"
            )
        return RiskResult(True, ())
    except (AgentError, TypeError, KeyError, ValueError, ArithmeticError) as exc:
        return RiskResult(
            False,
            (str(exc) if isinstance(exc, AgentError) else "Invalid intraday entry economics",),
        )
