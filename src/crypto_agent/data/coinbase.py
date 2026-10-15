"""Public Coinbase minute candles, isolated from all trading credentials."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

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
        self.timeout_seconds = timeout_seconds
        self.client = httpx.Client(timeout=timeout_seconds, follow_redirects=False, transport=transport)

    def get_bars(self, symbol, *, timeframe="1Min", limit=60):
        symbol = normalize_symbol(symbol)
        if timeframe != "1Min" or type(limit) is not int or not 30 <= limit <= 481:
            raise BrokerError("Coinbase bars require timeframe 1Min and limit in [30, 481]")
        # Explicit moving bounds avoid cached default windows. Never fabricate
        # missing candles, use a still-open candle, or mix providers in a window.
        end = utcnow().replace(second=0, microsecond=0)
        start = end - timedelta(minutes=limit + 2)
        try:
            raw = []
            deadline = monotonic() + self.timeout_seconds
            for chunk_start, chunk_end in self._windows(start, end):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise BrokerReadUnavailable()
                response = self.client.get(
                    f"https://api.exchange.coinbase.com/products/{symbol.replace('/', '-')}/candles",
                    params={
                        "granularity": 60,
                        "start": chunk_start.isoformat(),
                        "end": chunk_end.isoformat(),
                    },
                    timeout=remaining,
                )
                if monotonic() > deadline:
                    raise BrokerReadUnavailable()
                if response.status_code in {408, 429, 500, 502, 503, 504}:
                    raise BrokerReadUnavailable()
                if not response.is_success:
                    raise BrokerError(f"Coinbase public candles failed (HTTP {response.status_code})")
                page = json.loads(response.content, parse_float=Decimal)
                if not isinstance(page, list) or not page or len(page) > 300:
                    raise ValueError
                raw.append((chunk_start, chunk_end, page))
            if monotonic() > deadline:
                raise BrokerReadUnavailable()
        except httpx.HTTPError:
            raise BrokerReadUnavailable() from None
        except (ValueError, TypeError, OverflowError, OSError) as exc:
            raise BrokerError("Coinbase minute candles are missing or invalid") from exc
        try:
            bars_by_minute = {}
            last_boundary = None
            for _, chunk_end, page in raw:
                page_seen = set()
                for row in page:
                    if not isinstance(row, list) or len(row) != 6:
                        raise ValueError
                    seconds = row[0]
                    if type(seconds) is not int or seconds % 60:
                        raise ValueError
                    observed = datetime.fromtimestamp(seconds, UTC)
                    if observed in page_seen:
                        raise ValueError
                    page_seen.add(observed)
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
                        bar = PriceBar(symbol, observed, opened, high, low, closed, volume, SOURCE)
                        existing = bars_by_minute.get(observed)
                        if existing is not None:
                            if observed != last_boundary or existing != bar:
                                raise ValueError
                        else:
                            bars_by_minute[observed] = bar
                last_boundary = chunk_end
            bars = sorted(bars_by_minute.values(), key=lambda bar: bar.observed_at)
            if not bars:
                raise ValueError
            return tuple(bars[-limit:])
        except (AgentError, ValueError, TypeError, OverflowError, OSError) as exc:
            raise BrokerError("Coinbase minute candles are missing or invalid") from exc

    @staticmethod
    def _windows(start: datetime, end: datetime):
        if end - start <= timedelta(minutes=290):
            return ((start, end),)
        windows = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(minutes=290), end)
            windows.append((cursor, chunk_end))
            cursor = chunk_end
        return tuple(windows)

    def close(self):
        self.client.close()
