"""Alpaca REST adapter restricted to the exact Paper trading origin."""

import json
import math
import time
from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from crypto_agent.data.market import quote_snapshot
from crypto_agent.models import (
    SYMBOL,
    Activity,
    AgentError,
    AssetRules,
    BrokerError,
    BrokerOrder,
    BrokerRateLimited,
    BrokerReadUnavailable,
    MarketSnapshot,
    OrderIntent,
    OrderRejected,
    PortfolioSnapshot,
    Position,
    PriceBar,
    SubmissionUnknown,
    decimal,
    normalize_symbol,
    timestamp,
    utcnow,
)

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


def _retry_after_seconds(value: str | None, default: int = 60) -> int:
    """Bound an untrusted Retry-After header without exposing it in errors."""
    if value is None:
        return default
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            seconds = int(value)
        else:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                return default
            seconds = math.ceil((retry_at - utcnow()).total_seconds())
        return max(1, min(86400, seconds))
    except (TypeError, ValueError, OverflowError):
        return default


def parse_order(data: dict) -> BrokerOrder:
    try:
        qty = decimal(data["qty"], "order quantity")
        filled = decimal(data["filled_qty"], "filled quantity")
        price = decimal(data["filled_avg_price"], "fill price") if data.get("filled_avg_price") else None
        limit = decimal(data["limit_price"], "limit price") if data.get("limit_price") else None
        side = data["side"]
        if side not in {"buy", "sell"} or qty <= 0 or not Decimal(0) <= filled <= qty:
            raise ValueError
        if filled > 0 and (price is None or price <= 0):
            raise ValueError
        if not data["id"] or not data["client_order_id"] or not data["status"]:
            raise ValueError
        return BrokerOrder(
            str(data["id"]),
            str(data["client_order_id"]),
            normalize_symbol(data["symbol"]),
            side,
            qty,
            filled,
            price,
            str(data["status"]),
            timestamp(data.get("updated_at") or data["created_at"]),
            limit,
        )
    except (KeyError, TypeError, ValueError, AgentError) as exc:
        raise BrokerError("Broker returned an invalid or unsupported order") from exc


class AlpacaPaperBroker:
    mode = "paper"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        secret_key: str,
        timeout_seconds: int = 15,
        allow_submit: bool = False,
        symbols: tuple[str, ...] = (SYMBOL,),
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        # Reject paths, query strings, credentials, ports and lookalike hosts.
        if base_url.rstrip("/") != PAPER_URL:
            raise BrokerError("Only https://paper-api.alpaca.markets is permitted; live trading is disabled")
        if not api_key or not secret_key:
            raise BrokerError("Missing local Alpaca Paper API credentials")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise BrokerError("Broker request timeout must be in (0, 120] seconds")
        self.base_url = PAPER_URL
        self.allow_submit = allow_submit
        self.symbols = tuple(normalize_symbol(symbol) for symbol in symbols)
        if not 1 <= len(self.symbols) <= 3 or len(set(self.symbols)) != len(self.symbols):
            raise BrokerError("Broker requires one to three distinct supported symbols")
        self._client = httpx.Client(
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def _request(self, method, path, *, params=None, payload=None, data=False, missing_ok=False):
        origin = DATA_URL if data else PAPER_URL
        for attempt in range(3):
            try:
                response = self._client.request(method, origin + path, params=params, json=payload)
            except httpx.HTTPError as exc:
                if method == "GET" and isinstance(exc, httpx.TransportError):
                    if attempt == 2:
                        raise BrokerReadUnavailable() from None
                    time.sleep(attempt + 1)
                    continue
                if method == "POST":
                    raise SubmissionUnknown(
                        "Paper submission timed out or disconnected; query the client order ID"
                    ) from None
                raise BrokerError("Alpaca request timed out or disconnected") from None
            if method == "GET" and response.status_code in {408, 500, 502, 503, 504}:
                retry_after = response.headers.get("Retry-After")
                if retry_after is not None:
                    raise BrokerReadUnavailable(_retry_after_seconds(retry_after, default=300))
                if attempt == 2:
                    raise BrokerReadUnavailable()
                time.sleep(attempt + 1)
                continue
            break
        if missing_ok and response.status_code == 404:
            return None
        if not response.is_success:
            code = response.status_code
            if method == "GET" and code == 429:
                raise BrokerRateLimited(_retry_after_seconds(response.headers.get("Retry-After")))
            duplicate = False
            if method == "POST" and code == 422:
                try:
                    message = response.json().get("message", "").lower()
                    duplicate = "client_order_id" in message and (
                        "unique" in message or "duplicate" in message
                    )
                except (ValueError, TypeError, AttributeError):
                    # An unreadable response is not proof that this ID was rejected.
                    duplicate = True
            if method == "POST" and (code >= 500 or code in {408, 409, 429} or duplicate):
                # Duplicate IDs and uncertain server/gateway failures require lookup.
                raise SubmissionUnknown(f"Paper submission requires reconciliation (HTTP {code})")
            if method == "POST" and 400 <= code < 500:
                raise OrderRejected(f"Paper order rejected (HTTP {code})")
            raise BrokerError(f"Alpaca request failed (HTTP {code})")
        try:
            return json.loads(response.content, parse_float=Decimal) if response.content else None
        except (ValueError, TypeError):
            if method == "POST":
                raise SubmissionUnknown(
                    "Paper submission response is invalid; query the client order ID"
                ) from None
            raise BrokerError("Alpaca returned invalid JSON") from None

    def get_markets(self, symbols: tuple[str, ...]) -> dict[str, MarketSnapshot]:
        normalized = tuple(normalize_symbol(symbol) for symbol in symbols)
        if not normalized or len(set(normalized)) != len(normalized) or len(normalized) > 3:
            raise BrokerError("Market request requires one to three distinct supported symbols")
        if any(symbol not in self.symbols for symbol in normalized):
            raise BrokerError("Market request is outside the configured broker universe")
        payload = self._request(
            "GET",
            "/v1beta3/crypto/us/latest/orderbooks",
            params={"symbols": ",".join(normalized)},
            data=True,
        )
        try:
            markets = {}
            for symbol in normalized:
                book = payload["orderbooks"][symbol]
                bid, ask = book["b"][0], book["a"][0]
                if decimal(bid["s"], "bid size") <= 0 or decimal(ask["s"], "ask size") <= 0:
                    raise ValueError
                markets[symbol] = quote_snapshot(
                    symbol,
                    {"bp": bid["p"], "ap": ask["p"], "t": book["t"]},
                    "alpaca-crypto-us-orderbook",
                )
            return markets
        except (KeyError, IndexError, TypeError, ValueError, AgentError) as exc:
            raise BrokerError("Alpaca order book is missing or invalid") from exc

    def get_market(self, symbol: str = SYMBOL) -> MarketSnapshot:
        symbol = normalize_symbol(symbol)
        if symbol not in self.symbols:
            raise BrokerError("Asset request is outside the configured broker universe")
        return self.get_markets((symbol,))[symbol]

    def get_bars(
        self, symbol: str = SYMBOL, *, timeframe: str = "1Min", limit: int = 60
    ) -> tuple[PriceBar, ...]:
        """Read recent Alpaca US minute bars without substituting synthetic data."""
        symbol = normalize_symbol(symbol)
        if symbol not in self.symbols:
            raise BrokerError("Bar request is outside the configured broker universe")
        if timeframe != "1Min" or type(limit) is not int or not 30 <= limit <= 240:
            raise BrokerError("Intraday bars require timeframe 1Min and limit in [30, 240]")
        # A bounded start protects against a sparse page while ``sort=desc`` keeps
        # the latest observations in the single requested page.
        end = utcnow()
        start = end - timedelta(minutes=limit * 3)
        try:
            raw, page_token, seen_tokens = [], None, set()
            for _ in range(10):
                params = {
                    "symbols": symbol,
                    "timeframe": timeframe,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": limit,
                    "sort": "desc",
                }
                if page_token:
                    params["page_token"] = page_token
                payload = self._request("GET", "/v1beta3/crypto/us/bars", params=params, data=True)
                page = payload["bars"][symbol]
                if not isinstance(page, list):
                    raise ValueError
                raw.extend(page)
                page_token = payload.get("next_page_token")
                if len(raw) >= limit or not page_token:
                    break
                if page_token in seen_tokens:
                    raise ValueError
                seen_tokens.add(page_token)
            if not raw:
                raise ValueError
            bars = []
            for item in raw[:limit]:
                opened = decimal(item["o"], "bar open")
                high = decimal(item["h"], "bar high")
                low = decimal(item["l"], "bar low")
                closed = decimal(item["c"], "bar close")
                volume = decimal(item["v"], "bar volume")
                if (
                    min(opened, high, low, closed) <= 0
                    or volume < 0
                    or low > min(opened, closed)
                    or high < max(opened, closed)
                    or low > high
                ):
                    raise ValueError
                bars.append(
                    PriceBar(
                        symbol,
                        timestamp(item["t"]),
                        opened,
                        high,
                        low,
                        closed,
                        volume,
                        "alpaca-crypto-us-1min-bars",
                    )
                )
            bars.sort(key=lambda bar: bar.observed_at)
            if len({bar.observed_at for bar in bars}) != len(bars):
                raise ValueError
            return tuple(bars)
        except (KeyError, TypeError, ValueError, AgentError) as exc:
            raise BrokerError("Alpaca minute bars are missing or invalid") from exc

    def get_asset_rules(self, symbol: str = SYMBOL) -> AssetRules:
        symbol = normalize_symbol(symbol)
        data = self._request("GET", "/v2/assets/" + quote(symbol, safe=""))
        try:
            rules = AssetRules(
                normalize_symbol(data["symbol"]),
                decimal(data["min_order_size"]),
                decimal(data["min_trade_increment"]),
                decimal(data["price_increment"]),
                data["tradable"] is True and data["status"] == "active" and data["class"] == "crypto",
            )
            if min(rules.min_order_size, rules.quantity_increment, rules.price_increment) <= 0:
                raise ValueError
            return rules
        except (KeyError, TypeError, ValueError, AgentError) as exc:
            raise BrokerError("Alpaca asset rules are missing or invalid") from exc

    def get_portfolio(self) -> PortfolioSnapshot:
        # Timestamp the start: sequential reads cannot masquerade as an atomic snapshot.
        observed_at = utcnow()
        account = self._request("GET", "/v2/account")
        positions_data = self._request("GET", "/v2/positions")
        orders_data = self._request("GET", "/v2/orders", params={"status": "open", "limit": 500})
        try:
            if not isinstance(positions_data, list) or not isinstance(orders_data, list):
                raise ValueError
            if len(orders_data) >= 500:
                raise BrokerError("Open-order result is capped; refusing an incomplete account snapshot")
            positions = []
            for item in positions_data:
                quantity = decimal(item["qty"])
                available = decimal(item["qty_available"])
                entry = decimal(item["avg_entry_price"])
                if quantity < 0 or not Decimal(0) <= available <= quantity or entry <= 0:
                    raise ValueError
                item_symbol = normalize_symbol(item["symbol"])
                if item_symbol not in self.symbols:
                    raise ValueError
                positions.append(Position(item_symbol, quantity, entry, available))
            if account["currency"] != "USD":
                raise ValueError
            cash, equity = decimal(account["cash"]), decimal(account["equity"])
            buying_power = decimal(account["non_marginable_buying_power"])
            if cash < 0 or equity <= 0 or buying_power < 0:
                raise ValueError
            tradable = (
                account["status"] == "ACTIVE"
                and account.get("crypto_status") == "ACTIVE"
                and account.get("trading_blocked", True) is False
                and account.get("account_blocked", True) is False
                and account.get("trade_suspended_by_user", True) is False
            )
            orders = tuple(parse_order(item) for item in orders_data)
            if any(order.symbol not in self.symbols for order in orders):
                raise ValueError
            return PortfolioSnapshot(
                cash,
                equity,
                tuple(positions),
                observed_at,
                min(cash, buying_power),
                orders,
                tradable,
                str(account["id"]),
            )
        except BrokerError:
            raise
        except (KeyError, TypeError, ValueError, AgentError) as exc:
            raise BrokerError("Alpaca account snapshot is invalid or contains unsupported assets") from exc

    def submit_order(self, order: OrderIntent) -> BrokerOrder:
        if not self.allow_submit:
            raise OrderRejected("Paper writes are disabled; explicitly enable Paper execution")
        normalize_symbol(order.symbol)
        if order.symbol not in self.symbols:
            raise OrderRejected("Order symbol is outside the configured broker universe")
        if (
            order.side not in {"buy", "sell"}
            or order.time_in_force not in {"gtc", "ioc"}
            or not order.quantity.is_finite()
            or order.quantity <= 0
            or not order.limit_price.is_finite()
            or order.limit_price <= 0
            or not order.client_order_id
            or len(order.client_order_id) > 48
        ):
            raise OrderRejected("Invalid supported crypto/USD spot limit order")
        response = self._request(
            "POST",
            "/v2/orders",
            payload={
                "symbol": normalize_symbol(order.symbol),
                "qty": str(order.quantity),
                "side": order.side,
                "type": "limit",
                "limit_price": str(order.limit_price),
                "time_in_force": order.time_in_force,
                "client_order_id": order.client_order_id,
            },
        )
        try:
            return parse_order(response)
        except BrokerError:
            raise SubmissionUnknown(
                "Paper submission response cannot be parsed; query the client order ID"
            ) from None

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        response = self._request(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
            missing_ok=True,
        )
        return parse_order(response) if response is not None else None

    def cancel_order(self, client_order_id: str) -> None:
        if not self.allow_submit:
            raise OrderRejected("Paper writes are disabled; explicitly enable Paper execution")
        order = self.get_order(client_order_id)
        if order is None:
            raise BrokerError("Paper order was not found; cancellation cannot be confirmed")
        self._request("DELETE", "/v2/orders/" + quote(order.broker_order_id, safe=""))

    def get_activities(self, after: datetime | None = None) -> list[Activity]:
        params = {"activity_types": "FILL,CFEE,FEE", "direction": "asc", "page_size": 100}
        tracking_start = timestamp(after) if after is not None else None
        if after is not None:
            # Fee records can have only an activity date. Retrieve the entire
            # first day and keep that precision, then filter exact-time fills.
            params["after"] = tracking_start.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        results, seen = [], set()
        while True:
            page = self._request("GET", "/v2/account/activities", params=params)
            if not isinstance(page, list):
                raise BrokerError("Invalid Alpaca activity response")
            for item in page:
                activity = _parse_activity(item)
                if activity.activity_id in seen:
                    raise BrokerError("Alpaca activities pagination did not advance")
                seen.add(activity.activity_id)
                if tracking_start is not None:
                    if activity.time_precision == "day":
                        if activity.occurred_at.date() < tracking_start.date():
                            continue
                    elif activity.occurred_at <= tracking_start:
                        continue
                results.append(activity)
            if len(page) < 100:
                return results
            params["page_token"] = page[-1]["id"]

    def close(self) -> None:
        self._client.close()


def _parse_activity(data: dict) -> Activity:
    try:
        kind = data["activity_type"]
        symbol = normalize_symbol(data["symbol"]) if data.get("symbol") else None
        occurred = data.get("transaction_time") or data.get("date")
        precision = "instant"
        if isinstance(occurred, str) and len(occurred) == 10:
            precision = "day"
            occurred += "T00:00:00+00:00"
        qty = decimal(data.get("qty", 0))
        price = decimal(data.get("price", 0))
        fee = None
        currency = str(data["currency"]).upper() if data.get("currency") else None
        if kind == "FILL":
            if (
                precision != "instant"
                or qty <= 0
                or price <= 0
                or data.get("side") not in {"buy", "sell"}
                or not data.get("order_id")
            ):
                raise ValueError
        elif kind in {"CFEE", "FEE"}:
            if qty != 0 and price > 0:
                fee = abs(qty * price)
            elif (
                data.get("net_amount") is not None
                and decimal(data["net_amount"]) != 0
                and currency in {None, "USD"}
            ):
                fee = abs(decimal(data["net_amount"]))
            elif data.get("net_amount") is not None and qty == 0 and currency in {None, "USD"}:
                fee = Decimal(0)
        else:
            raise ValueError
        return Activity(
            str(data["id"]),
            kind,
            timestamp(occurred),
            symbol,
            data.get("order_id"),
            data.get("side"),
            qty,
            price,
            fee,
            precision,
            currency,
        )
    except (KeyError, TypeError, ValueError, AgentError) as exc:
        raise BrokerError("Alpaca returned an invalid activity") from exc
