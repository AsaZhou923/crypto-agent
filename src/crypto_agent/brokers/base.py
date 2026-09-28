"""Contracts shared by paper trading and the explicitly offline test broker."""

from datetime import datetime
from typing import Protocol

from crypto_agent.models import (
    Activity,
    AssetRules,
    BrokerOrder,
    MarketSnapshot,
    OrderIntent,
    PortfolioSnapshot,
    PriceBar,
)


class Broker(Protocol):
    mode: str

    def get_market(self, symbol: str = ...) -> MarketSnapshot: ...

    def get_markets(self, symbols: tuple[str, ...]) -> dict[str, MarketSnapshot]: ...

    def get_bars(
        self, symbol: str = ..., *, timeframe: str = ..., limit: int = ...
    ) -> tuple[PriceBar, ...]: ...

    def get_asset_rules(self, symbol: str = ...) -> AssetRules: ...

    def get_portfolio(self) -> PortfolioSnapshot: ...

    def submit_order(self, order: OrderIntent) -> BrokerOrder: ...

    def get_order(self, client_order_id: str) -> BrokerOrder | None: ...

    def cancel_order(self, client_order_id: str) -> None: ...

    def get_activities(self, after: datetime | None = None) -> list[Activity]: ...

    def close(self) -> None: ...
