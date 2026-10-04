"""Public Coinbase minute candles, isolated from all trading credentials."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from crypto_agent.models import (
    AgentError,
    BrokerError,
    BrokerReadUnavailable,
    PriceBar,
    decimal,
    normalize_symbol,
    utcnow,
)

SOURCE = "coinbase-exchange-1min-bars"


class CoinbaseMinuteBars:
    def __init__(self, timeout_seconds=15, *, transport=None):
        self.client = httpx.Client(timeout=timeout_seconds, follow_redirects=False, transport=transport)

    def get_bars(self, symbol, *, timeframe="1Min", limit=60):
        symbol = normalize_symbol(symbol)
        if timeframe != "1Min" or type(limit) is not int or not 30 <= limit <= 240:
            raise BrokerError("Coinbase bars require timeframe 1Min and limit in [30, 240]")
        # Explicit moving bounds avoid cached default windows. Never fabricate
        # missing candles, use a still-open candle, or mix providers in a window.
        end = utcnow().replace(second=0, microsecond=0)
        start = end - timedelta(minutes=limit + 2)
        try:
            response = self.client.get(
                f"https://api.exchange.coinbase.com/products/{symbol.replace('/', '-')}/candles",
                params={"granularity": 60, "start": start.isoformat(), "end": end.isoformat()},
            )
        except httpx.HTTPError:
            raise BrokerReadUnavailable() from None
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            raise BrokerReadUnavailable()
        if not response.is_success:
            raise BrokerError(f"Coinbase public candles failed (HTTP {response.status_code})")
        try:
            raw = json.loads(response.content, parse_float=Decimal)
            if not isinstance(raw, list) or not raw or len(raw) > 300:
                raise ValueError
            bars = []
            for row in raw:
                if not isinstance(row, list) or len(row) != 6:
                    raise ValueError
                seconds = row[0]
                if type(seconds) is not int or seconds % 60:
                    raise ValueError
                observed = datetime.fromtimestamp(seconds, UTC)
                low, high, opened, closed, volume = (decimal(value) for value in row[1:])
                if (
                    min(low, high, opened, closed) <= 0
                    or volume < 0
                    or not low <= min(opened, closed) <= max(opened, closed) <= high
                ):
                    raise ValueError
                if observed > end:
                    raise ValueError
                if start <= observed and observed + timedelta(minutes=1) <= end:
                    bars.append(PriceBar(symbol, observed, opened, high, low, closed, volume, SOURCE))
            bars.sort(key=lambda bar: bar.observed_at)
            if not bars or len({bar.observed_at for bar in bars}) != len(bars):
                raise ValueError
            return tuple(bars[-limit:])
        except (AgentError, ValueError, TypeError, OverflowError, OSError) as exc:
            raise BrokerError("Coinbase minute candles are missing or invalid") from exc

    def close(self):
        self.client.close()
