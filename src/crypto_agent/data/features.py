"""Validated, deterministic features for the 10-minute intraday decision loop."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from crypto_agent.models import (
    AgentError,
    MarketSnapshot,
    PriceBar,
    decimal,
    normalize_symbol,
    timestamp,
    utcnow,
)

BPS = Decimal(10000)


@dataclass(frozen=True)
class IntradayFeatures:
    symbol: str
    timeframe: str
    bar_count: int
    first_bar_at: datetime
    last_bar_at: datetime
    last_close: Decimal
    quote_price: Decimal
    quote_vs_close_bps: Decimal
    return_3m_bps: Decimal
    return_10m_bps: Decimal
    return_30m_bps: Decimal
    ema_5_vs_20_bps: Decimal
    realized_vol_20m_bps: Decimal
    range_20m_bps: Decimal
    recent_volume_ratio: Decimal | None
    momentum_score: int
    source: str

    def payload(self) -> dict:
        return asdict(self)


def _return_bps(closes: list[Decimal], intervals: int) -> Decimal:
    return (closes[-1] / closes[-1 - intervals] - 1) * BPS


def _ema(values: list[Decimal], period: int) -> Decimal:
    alpha = Decimal(2) / Decimal(period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * alpha + result * (1 - alpha)
    return result


def _vote(value: Decimal, threshold: Decimal) -> int:
    if value >= threshold:
        return 1
    if value <= -threshold:
        return -1
    return 0


def build_intraday_features(
    market: MarketSnapshot,
    bars: tuple[PriceBar, ...],
    *,
    lookback_bars: int,
    min_bars: int,
    max_age_seconds: int,
    momentum_threshold_bps: Decimal,
    now: datetime | None = None,
    expected_source: str | None = None,
    max_quote_bar_deviation_bps: Decimal | None = None,
) -> IntradayFeatures:
    """Validate closed 1-minute bars and compute a small auditable feature set."""
    now = timestamp(now or utcnow())
    symbol = normalize_symbol(market.symbol)
    threshold = decimal(momentum_threshold_bps, "intraday momentum threshold")
    if (
        type(lookback_bars) is not int
        or type(min_bars) is not int
        or type(max_age_seconds) is not int
        or not 31 <= min_bars <= lookback_bars <= 240
        or not 1 <= max_age_seconds <= 600
        or threshold <= 0
    ):
        raise AgentError("Invalid intraday feature window configuration")
    if not isinstance(bars, tuple) or not bars:
        raise AgentError("Intraday minute bars are missing")

    validated = []
    sources = set()
    for bar in bars:
        if not isinstance(bar, PriceBar) or normalize_symbol(bar.symbol) != symbol:
            raise AgentError("Intraday bars contain the wrong symbol or schema")
        observed = timestamp(bar.observed_at)
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if any(not value.is_finite() for value in values):
            raise AgentError("Intraday bars contain non-finite values")
        if (
            min(bar.open, bar.high, bar.low, bar.close) <= 0
            or bar.volume < 0
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
        ):
            raise AgentError("Intraday bars contain invalid OHLCV values")
        if observed > now + timedelta(seconds=5):
            raise AgentError("Intraday bars contain a future timestamp")
        sources.add(bar.source)
        # A bar timestamp is the interval start. Never use a still-open minute.
        if observed + timedelta(minutes=1) <= now:
            validated.append(bar)
    if len(sources) != 1:
        raise AgentError("Intraday bars mix data sources")
    if expected_source is not None and sources != {expected_source}:
        raise AgentError("Intraday bars do not match configured source")
    validated.sort(key=lambda bar: bar.observed_at)
    if len({bar.observed_at for bar in validated}) != len(validated):
        raise AgentError("Intraday bars contain duplicate timestamps")
    validated = validated[-lookback_bars:]
    if len(validated) < min_bars:
        raise AgentError("Too few closed intraday minute bars")
    for previous, current in zip(validated, validated[1:], strict=False):
        gap = (timestamp(current.observed_at) - timestamp(previous.observed_at)).total_seconds()
        # Index-based returns and EMA periods represent minutes only when
        # every selected, closed bar follows the previous one by exactly 60s.
        if gap != 60:
            raise AgentError("Intraday minute bars must be consecutive 60-second intervals; gapped data")
    latest_end = timestamp(validated[-1].observed_at) + timedelta(minutes=1)
    age = (now - latest_end).total_seconds()
    if age < 0 or age > max_age_seconds:
        raise AgentError("Intraday minute bars are stale")

    closes = [bar.close for bar in validated]
    if max_quote_bar_deviation_bps is not None:
        deviation_limit = decimal(max_quote_bar_deviation_bps)
        if not 1 <= deviation_limit <= 100:
            raise AgentError("Invalid cross-venue price deviation limit")
        if abs(market.price / closes[-1] - 1) * BPS > deviation_limit:
            raise AgentError("Execution quote diverges from analysis candles beyond configured limit")
    return_3m = _return_bps(closes, 3)
    return_10m = _return_bps(closes, 10)
    return_30m = _return_bps(closes, 30)
    ema_spread = (_ema(closes, 5) / _ema(closes, 20) - 1) * BPS
    one_minute_returns = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 19, len(closes))]
    mean_square = sum((value * value for value in one_minute_returns), Decimal(0)) / Decimal(
        len(one_minute_returns)
    )
    realized_vol = mean_square.sqrt() * BPS
    last_twenty = validated[-20:]
    price_range = (
        (max(bar.high for bar in last_twenty) - min(bar.low for bar in last_twenty)) / closes[-1] * BPS
    )
    recent_volume = sum((bar.volume for bar in validated[-5:]), Decimal(0)) / Decimal(5)
    previous_volume = sum((bar.volume for bar in validated[-25:-5]), Decimal(0)) / Decimal(20)
    volume_ratio = recent_volume / previous_volume if previous_volume > 0 else None
    score = sum(
        (
            _vote(return_3m, threshold),
            _vote(return_10m, threshold * Decimal("1.5")),
            _vote(return_30m, threshold * Decimal(2)),
            _vote(ema_spread, threshold * Decimal("0.75")),
        )
    )
    return IntradayFeatures(
        symbol=symbol,
        timeframe="1Min",
        bar_count=len(validated),
        first_bar_at=timestamp(validated[0].observed_at),
        last_bar_at=timestamp(validated[-1].observed_at),
        last_close=closes[-1],
        quote_price=market.price,
        quote_vs_close_bps=(market.price / closes[-1] - 1) * BPS,
        return_3m_bps=return_3m,
        return_10m_bps=return_10m,
        return_30m_bps=return_30m,
        ema_5_vs_20_bps=ema_spread,
        realized_vol_20m_bps=realized_vol,
        range_20m_bps=price_range,
        recent_volume_ratio=volume_ratio,
        momentum_score=score,
        source=validated[-1].source,
    )
