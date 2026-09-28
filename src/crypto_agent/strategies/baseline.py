"""Deterministic threshold rule for plumbing checks and comparison.

This deliberately makes no forecast and does not imply profitable performance.
The separate risk engine and planner decide whether rebalancing is permitted.
"""

from datetime import timedelta
from decimal import Decimal

from crypto_agent.models import (
    AgentError,
    MarketSnapshot,
    PortfolioSnapshot,
    TradeDecision,
    decimal,
    normalize_symbol,
    utcnow,
)


class BaselineStrategy:
    version = "price-threshold-v1"

    def __init__(
        self,
        config: dict,
    ):
        self.target_position_pct = decimal(config.get("baseline_target_pct"), "baseline target allocation")
        self.buy_below_usd = decimal(config.get("baseline_buy_below_usd"), "baseline price threshold")
        if self.buy_below_usd <= 0:
            raise AgentError("Baseline price threshold must be positive")
        if not 0 <= self.target_position_pct <= 1:
            raise AgentError("Baseline target allocation must be between 0 and 1")
        decision_ttl_seconds = config.get("decision_ttl_seconds")
        if (
            isinstance(decision_ttl_seconds, bool)
            or not isinstance(decision_ttl_seconds, int)
            or not 1 <= decision_ttl_seconds <= 86400
        ):
            raise AgentError("Decision TTL must be an integer between 1 and 86400 seconds")
        self.decision_ttl_seconds = decision_ttl_seconds

    def decide(self, market: MarketSnapshot, portfolio: PortfolioSnapshot) -> TradeDecision:
        now = utcnow()
        normalize_symbol(market.symbol)
        target = self.target_position_pct if market.price <= self.buy_below_usd else Decimal(0)
        return TradeDecision(
            symbol=market.symbol,
            target_position_pct=target,
            reason="Deterministic price-threshold rebalance rule; no return forecast.",
            expires_at=now + timedelta(seconds=self.decision_ttl_seconds),
            created_at=now,
            rating="REBALANCE",
            strategy_version=self.version,
            model="none",
            evidence=(
                f"Quote {market.price} USD at {market.observed_at.isoformat()} ({market.source})",
                f"Account equity {portfolio.equity_usd} USD; current {market.symbol} {portfolio.quantity_for(market.symbol)}",
                f"Price threshold {self.buy_below_usd} USD; target equity fraction {target}",
            ),
        )
