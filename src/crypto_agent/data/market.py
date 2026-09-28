"""Validate provider quotes without substituting synthetic market data."""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from crypto_agent.models import (
    AgentError,
    MarketSnapshot,
    decimal,
    normalize_symbol,
    timestamp,
    utcnow,
)


def quote_snapshot(symbol: str, quote: Mapping, source: str) -> MarketSnapshot:
    """Preserve decimal quote precision; round only when constructing an order."""
    try:
        bid = decimal(quote["bp"], "bid")
        ask = decimal(quote["ap"], "ask")
        observed_at = timestamp(quote["t"])
    except (KeyError, TypeError) as exc:
        raise AgentError("Market quote is missing required bid, ask or timestamp") from exc
    if bid <= 0 or ask <= 0 or ask < bid:
        raise AgentError("Market quote has nonpositive or crossed prices")
    return MarketSnapshot(
        normalize_symbol(symbol),
        (bid + ask) / Decimal(2),
        observed_at,
        bid,
        ask,
        source,
    )


def validate_snapshot(
    snapshot: MarketSnapshot,
    *,
    max_age_seconds: int,
    max_spread_bps: Decimal,
    now: datetime | None = None,
) -> None:
    """Bounds are supplied by risk configuration, not implicit execution defaults."""
    normalize_symbol(snapshot.symbol)
    if any(not p.is_finite() or p <= 0 for p in (snapshot.price, snapshot.bid, snapshot.ask)):
        raise AgentError("Market prices must be positive finite decimals")
    if snapshot.bid > snapshot.ask or not snapshot.bid <= snapshot.price <= snapshot.ask:
        raise AgentError("Market prices are inconsistent")
    age = (timestamp(now or utcnow()) - timestamp(snapshot.observed_at)).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise AgentError("Market data is stale or has a future timestamp")
    if (snapshot.ask - snapshot.bid) / snapshot.price * Decimal(10000) > max_spread_bps:
        raise AgentError("Market spread exceeds configured limit")


def fetch_snapshot(symbol: str, broker) -> MarketSnapshot:
    normalize_symbol(symbol)
    return broker.get_market()
