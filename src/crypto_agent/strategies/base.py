"""Strategy contract shared by rules and AI implementations."""

from typing import Protocol

from crypto_agent.models import MarketSnapshot, PortfolioSnapshot, TradeDecision


class Strategy(Protocol):
    def decide(self, market: MarketSnapshot, portfolio: PortfolioSnapshot) -> TradeDecision: ...
