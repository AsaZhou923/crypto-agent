"""Broker-neutral contracts. Money and quantities never use binary floats."""

import json
import os
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

SUPPORTED_SYMBOLS = ("BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD")
SYMBOL = SUPPORTED_SYMBOLS[0]  # Backward-compatible default for the offline demo.
UPSTREAM_COMMIT = "2d17df8da1536c121e4d7395ac5a5dcec9e96d6f"
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})


class AgentError(Exception):
    """Safe, user-facing error: never include credentials or raw HTTP bodies."""


class BrokerError(AgentError):
    pass


class BrokerRateLimited(BrokerError):
    """A read-only broker request was throttled; defer without retrying it."""

    def __init__(self, retry_after_seconds: int = 60):
        super().__init__("Alpaca read request rate limited (HTTP 429)")
        self.retry_after_seconds = max(1, min(86400, retry_after_seconds))


class BrokerReadUnavailable(BrokerError):
    """A transient read failure persisted; defer to a fresh trading round."""

    def __init__(self, retry_after_seconds: int = 300):
        super().__init__("Alpaca read request temporarily unavailable")
        self.retry_after_seconds = max(1, min(86400, retry_after_seconds))


class SubmissionUnknown(BrokerError):
    """Submission may have reached the broker; reconcile, never blindly retry."""


class OrderRejected(BrokerError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def timestamp(value: str | datetime) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(UTC)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AgentError("Invalid or timezone-naive timestamp") from exc


def decimal(value, label: str = "number") -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AgentError(f"Invalid finite decimal: {label}") from exc


def normalize_symbol(value: str) -> str:
    if not isinstance(value, str):
        raise AgentError("Unsupported crypto symbol")
    compact = value.upper().replace("/", "").replace("-", "")
    matches = [symbol for symbol in SUPPORTED_SYMBOLS if symbol.replace("/", "") == compact]
    if len(matches) != 1:
        raise AgentError("Unsupported configured crypto/USD spot symbol")
    return matches[0]


def upstream_symbol(value: str) -> str:
    return normalize_symbol(value).replace("/", "-")


def json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return timestamp(value).isoformat()
    raise TypeError(type(value).__name__)


def dumps(value) -> str:
    serialized = json.dumps(value, default=json_default, ensure_ascii=False, sort_keys=True)
    # Last boundary before JSON leaves the process or is persisted. Raw SDK
    # errors are suppressed separately; this also protects model-generated text.
    for key, secret in os.environ.items():
        if (
            any(part in key.upper() for part in ("API_KEY", "SECRET", "TOKEN", "PASSWORD"))
            and len(secret) >= 8
        ):
            escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
            serialized = serialized.replace(escaped, "[REDACTED]")
            serialized = serialized.replace(secret, "[REDACTED]")
    return serialized


@dataclass(frozen=True)
class AssetRules:
    symbol: str
    min_order_size: Decimal
    quantity_increment: Decimal
    price_increment: Decimal
    tradable: bool = True


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    price: Decimal
    observed_at: datetime
    bid: Decimal
    ask: Decimal
    source: str


@dataclass(frozen=True)
class PriceBar:
    """One provider bar. ``observed_at`` is the UTC start of its interval."""

    symbol: str
    observed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    available_quantity: Decimal


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    filled_quantity: Decimal
    filled_avg_price: Decimal | None
    status: str
    updated_at: datetime
    limit_price: Decimal | None = None

    @property
    def remaining_quantity(self) -> Decimal:
        return max(Decimal(0), self.quantity - self.filled_quantity)


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash_usd: Decimal
    equity_usd: Decimal
    positions: tuple[Position, ...]
    observed_at: datetime
    buying_power_usd: Decimal
    open_orders: tuple[BrokerOrder, ...] = ()
    tradable: bool = True
    account_id: str = "offline-test-account"

    @property
    def btc_quantity(self) -> Decimal:
        return sum((p.quantity for p in self.positions if p.symbol == SYMBOL), Decimal(0))

    def quantity_for(self, symbol: str) -> Decimal:
        symbol = normalize_symbol(symbol)
        return sum((p.quantity for p in self.positions if p.symbol == symbol), Decimal(0))

    def available_for(self, symbol: str) -> Decimal:
        symbol = normalize_symbol(symbol)
        return sum((p.available_quantity for p in self.positions if p.symbol == symbol), Decimal(0))


@dataclass(frozen=True)
class TradeDecision:
    symbol: str
    target_position_pct: Decimal | None
    reason: str
    expires_at: datetime
    created_at: datetime
    rating: str
    strategy_version: str
    model: str
    evidence: tuple[str, ...]
    actionable: bool = True
    evaluation_eligible: bool = True


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    client_order_id: str
    limit_price: Decimal
    estimated_notional_usd: Decimal
    estimated_fee_usd: Decimal
    time_in_force: str = "gtc"


@dataclass(frozen=True)
class Activity:
    activity_id: str
    kind: str  # FILL, CFEE or FEE
    occurred_at: datetime
    symbol: str | None = None
    order_id: str | None = None
    side: str | None = None
    quantity: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    fee_usd: Decimal | None = None
    time_precision: str = "instant"


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    reasons: tuple[str, ...]
