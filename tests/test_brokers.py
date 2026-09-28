"""Protocol tests with synthetic HTTP responses, not live Alpaca integration."""

import json
from datetime import timedelta
from decimal import Decimal as D
from email.utils import format_datetime

import httpx
import pytest

from crypto_agent.brokers.alpaca_paper import PAPER_URL, AlpacaPaperBroker
from crypto_agent.data.market import validate_snapshot
from crypto_agent.models import (
    AgentError,
    BrokerError,
    BrokerRateLimited,
    BrokerReadUnavailable,
    OrderIntent,
    OrderRejected,
    SubmissionUnknown,
    utcnow,
)


def order_intent():
    return OrderIntent("BTC/USD", "buy", D("0.01"), "test-client-id", D("50010"), D("500.10"), D("1.25025"))


def order_payload(**overrides):
    data = {
        "id": "broker-id",
        "client_order_id": "test-client-id",
        "symbol": "BTCUSD",
        "side": "buy",
        "qty": "0.01",
        "filled_qty": "0.005",
        "filled_avg_price": "50010",
        "status": "partially_filled",
        "updated_at": utcnow().isoformat(),
        "limit_price": "50010",
    }
    return data | overrides


def broker(handler, **options):
    return AlpacaPaperBroker(
        PAPER_URL, "TEST-KEY", "TEST-SECRET", transport=httpx.MockTransport(handler), **options
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.invalid",
        "https://paper-api.alpaca.markets@evil.invalid",
        "https://paper-api.alpaca.markets/v2",
        "https://paper-api.alpaca.markets:443",
    ],
)
def test_live_and_nonexact_origins_rejected(url):
    with pytest.raises(BrokerError, match="live trading is disabled"):
        AlpacaPaperBroker(url, "key", "secret")


def test_default_disables_both_writes():
    calls = []
    with_broker = broker(lambda request: calls.append(request))
    with pytest.raises(OrderRejected, match="disabled"):
        with_broker.submit_order(order_intent())
    with pytest.raises(OrderRejected, match="disabled"):
        with_broker.cancel_order("client")
    assert not calls


def test_redirect_is_rejected_without_credentials_following_it():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://api.alpaca.markets/v2/account"})

    with pytest.raises(BrokerError, match="HTTP 302"):
        broker(handler).get_portfolio()
    assert len(calls) == 1
    assert calls[0].url.host == "paper-api.alpaca.markets"


def test_timeout_is_unknown_and_never_blindly_retried():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("TEST-SECRET", request=request)
        return httpx.Response(200, json=order_payload())

    paper = broker(handler, allow_submit=True)
    with pytest.raises(SubmissionUnknown) as exc:
        paper.submit_order(order_intent())
    assert "TEST-SECRET" not in str(exc.value)
    assert len(calls) == 1
    found = paper.get_order("test-client-id")
    assert found.filled_quantity == D("0.005")
    assert found.status == "partially_filled"
    assert [item.method for item in calls] == ["POST", "GET"]


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError, 408, 500, 502, 503, 504])
@pytest.mark.parametrize("recovers", [False, True])
def test_transient_reads_retry_with_bounded_backoff(monkeypatch, failure, recovers):
    calls, sleeps = [], []
    monkeypatch.setattr("crypto_agent.brokers.alpaca_paper.time.sleep", sleeps.append)

    def handler(request):
        calls.append(request)
        if recovers and len(calls) == 3:
            return httpx.Response(200, json=order_payload())
        if isinstance(failure, int):
            return httpx.Response(failure, text="TEST-SECRET")
        raise failure("TEST-SECRET", request=request)

    paper = broker(handler)
    if recovers:
        assert paper.get_order("private-client-id").status == "partially_filled"
    else:
        with pytest.raises(BrokerReadUnavailable) as exc:
            paper.get_order("private-client-id")
        assert exc.value.retry_after_seconds == 300
        assert "TEST-SECRET" not in str(exc.value)
        assert "private-client-id" not in str(exc.value)
    assert len(calls) == 3
    assert all(request.method == "GET" for request in calls)
    assert sleeps == [1, 2]


@pytest.mark.parametrize("header, expected", [("900", 900), ("0", 1), ("999999999", 86400), ("bad", 300)])
def test_transient_read_retry_after_defers_without_sleep(monkeypatch, header, expected):
    calls, sleeps = [], []
    monkeypatch.setattr("crypto_agent.brokers.alpaca_paper.time.sleep", sleeps.append)

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": header})

    with pytest.raises(BrokerReadUnavailable) as exc:
        broker(handler).get_order("test-client-id")
    assert exc.value.retry_after_seconds == expected
    assert len(calls) == 1
    assert sleeps == []


def test_transient_read_accepts_retry_after_date(monkeypatch):
    now = utcnow().replace(microsecond=0)
    monkeypatch.setattr("crypto_agent.brokers.alpaca_paper.utcnow", lambda: now)
    header = format_datetime(now + timedelta(seconds=900), usegmt=True)
    with pytest.raises(BrokerReadUnavailable) as exc:
        broker(lambda _: httpx.Response(503, headers={"Retry-After": header})).get_order("test-client-id")
    assert exc.value.retry_after_seconds == 900


@pytest.mark.parametrize("method, expected", [("POST", SubmissionUnknown), ("DELETE", BrokerError)])
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, 408, 500, 502, 503, 504])
def test_transient_writes_never_retry(monkeypatch, method, expected, failure):
    calls, sleeps = [], []
    monkeypatch.setattr("crypto_agent.brokers.alpaca_paper.time.sleep", sleeps.append)

    def handler(request):
        calls.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure, headers={"Retry-After": "900"})
        raise failure("TEST-SECRET", request=request)

    with pytest.raises(expected) as exc:
        broker(handler, allow_submit=True)._request(method, "/v2/orders")
    assert type(exc.value) is expected
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "status, content", [(400, b"bad"), (401, b"bad"), (403, b"bad"), (501, b"bad"), (200, b"invalid JSON")]
)
def test_nontransient_read_failures_do_not_retry(monkeypatch, status, content):
    calls, sleeps = [], []
    monkeypatch.setattr("crypto_agent.brokers.alpaca_paper.time.sleep", sleeps.append)

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=content)

    with pytest.raises(BrokerError) as exc:
        broker(handler).get_order("test-client-id")
    assert type(exc.value) is BrokerError
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "retry_after, expected",
    [
        (None, 60),
        ("120", 120),
        ("0", 1),
        ("999999999", 86400),
        ("-1", 60),
        ("1.5", 60),
        ("NaN", 60),
        ("TEST-SECRET", 60),
    ],
)
def test_read_rate_limit_defers_once_with_safe_bounded_retry_after(retry_after, expected):
    calls = []

    def handler(request):
        calls.append(request)
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return httpx.Response(429, headers=headers, text="TEST-SECRET sensitive response")

    with pytest.raises(BrokerRateLimited) as exc:
        broker(handler).get_order("private-client-id")
    assert exc.value.retry_after_seconds == expected
    assert "TEST-SECRET" not in str(exc.value)
    assert "private-client-id" not in str(exc.value)
    assert len(calls) == 1 and calls[0].method == "GET"


@pytest.mark.parametrize("delay, expected", [(-120, 1), (120, 120), (172800, 86400)])
def test_read_rate_limit_accepts_http_date(monkeypatch, delay, expected):
    now = utcnow().replace(microsecond=0)
    monkeypatch.setattr("crypto_agent.brokers.alpaca_paper.utcnow", lambda: now)
    header = format_datetime(now + timedelta(seconds=delay), usegmt=True)
    with pytest.raises(BrokerRateLimited) as exc:
        broker(lambda _: httpx.Response(429, headers={"Retry-After": header})).get_portfolio()
    assert exc.value.retry_after_seconds == expected


def test_submit_rate_limit_remains_unknown_without_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    with pytest.raises(SubmissionUnknown):
        broker(handler, allow_submit=True).submit_order(order_intent())
    assert len(calls) == 1 and calls[0].method == "POST"


def test_cancel_rate_limit_is_not_a_read_deferral_and_never_retried():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=order_payload())
        return httpx.Response(429, headers={"Retry-After": "120"})

    with pytest.raises(BrokerError) as exc:
        broker(handler, allow_submit=True).cancel_order("test-client-id")
    assert type(exc.value) is BrokerError
    assert [item.method for item in calls] == ["GET", "DELETE"]


@pytest.mark.parametrize(
    "status, exception",
    [(400, OrderRejected), (403, OrderRejected), (500, SubmissionUnknown), (409, SubmissionUnknown)],
)
def test_submit_errors_are_safe_and_classified(status, exception):
    paper = broker(lambda _: httpx.Response(status, text="TEST-SECRET sensitive response"), allow_submit=True)
    with pytest.raises(exception) as exc:
        paper.submit_order(order_intent())
    assert "sensitive" not in str(exc.value) and "TEST-SECRET" not in str(exc.value)


def test_malformed_submit_response_is_unknown():
    paper = broker(lambda _: httpx.Response(200, json={}), allow_submit=True)
    with pytest.raises(SubmissionUnknown):
        paper.submit_order(order_intent())


def test_validation_rejection_is_distinct_from_duplicate_id():
    rejected = broker(
        lambda _: httpx.Response(422, json={"message": "insufficient balance"}), allow_submit=True
    )
    with pytest.raises(OrderRejected):
        rejected.submit_order(order_intent())
    duplicate = broker(
        lambda _: httpx.Response(422, json={"message": "client_order_id must be unique"}), allow_submit=True
    )
    with pytest.raises(SubmissionUnknown):
        duplicate.submit_order(order_intent())


def test_cancel_uses_client_id_lookup_and_authoritative_broker_id():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=order_payload()) if request.method == "GET" else httpx.Response(204)

    paper = broker(handler, allow_submit=True)
    paper.cancel_order("test-client-id")
    assert calls[0].url.params["client_order_id"] == "test-client-id"
    assert calls[1].method == "DELETE" and calls[1].url.path == "/v2/orders/broker-id"


def test_missing_order_is_none_but_failed_lookup_is_error():
    assert broker(lambda _: httpx.Response(404)).get_order("missing") is None
    with pytest.raises(BrokerError):
        broker(lambda _: httpx.Response(503)).get_order("missing")


def test_orderbook_preserves_decimal_and_timestamp_and_staleness_is_checked():
    old = utcnow() - timedelta(minutes=10)
    paper = broker(
        lambda _: httpx.Response(
            200,
            json={
                "orderbooks": {
                    "BTC/USD": {
                        "b": [{"p": "49999.123456789", "s": "1"}],
                        "a": [{"p": "50000.123456789", "s": "2"}],
                        "t": old.isoformat(),
                    }
                }
            },
        )
    )
    market = paper.get_market()
    assert market.bid == D("49999.123456789")
    assert market.observed_at == old
    with pytest.raises(AgentError, match="stale"):
        validate_snapshot(market, max_age_seconds=60, max_spread_bps=D(100))


def test_minute_bars_preserve_precision_and_request_latest_descending_window():
    calls = []
    now = utcnow().replace(second=0, microsecond=0)
    raw = [
        {
            "t": (now - timedelta(minutes=index + 1)).isoformat(),
            "o": "50000.123456789",
            "h": "50002.123456789",
            "l": "49999.123456789",
            "c": "50001.123456789",
            "v": "0",
        }
        for index in range(30)
    ]

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"bars": {"BTC/USD": raw}, "next_page_token": None})

    result = broker(handler).get_bars(limit=30)
    assert len(result) == 30
    assert result[0].observed_at < result[-1].observed_at
    assert result[-1].close == D("50001.123456789")
    assert calls[0].url.path == "/v1beta3/crypto/us/bars"
    assert calls[0].url.params["timeframe"] == "1Min"
    assert calls[0].url.params["sort"] == "desc"


@pytest.mark.parametrize(
    "bar",
    [
        {"t": "2026-09-19T00:00:00Z", "o": "1", "h": "2", "l": "0", "c": "1", "v": "1"},
        {"t": "2026-09-19T00:00:00Z", "o": "2", "h": "1", "l": "1", "c": "2", "v": "1"},
        {"t": "2026-09-19T00:00:00Z", "o": "1", "h": "2", "l": "1", "c": "2", "v": "-1"},
    ],
)
def test_invalid_minute_bars_fail_without_fallback(bar):
    paper = broker(lambda _: httpx.Response(200, json={"bars": {"BTC/USD": [bar]}}))
    with pytest.raises(BrokerError, match="minute bars"):
        paper.get_bars(limit=30)


def test_multi_symbol_orderbooks_and_order_use_exact_configured_symbol():
    calls = []
    now = utcnow().isoformat()

    def handler(request):
        calls.append(request)
        if request.url.host == "data.alpaca.markets":
            return httpx.Response(
                200,
                json={
                    "orderbooks": {
                        "BTC/USD": {
                            "b": [{"p": "50000", "s": "1"}],
                            "a": [{"p": "50001", "s": "1"}],
                            "t": now,
                        },
                        "ETH/USD": {
                            "b": [{"p": "2500", "s": "1"}],
                            "a": [{"p": "2501", "s": "1"}],
                            "t": now,
                        },
                        "SOL/USD": {
                            "b": [{"p": "100", "s": "1"}],
                            "a": [{"p": "100.1", "s": "1"}],
                            "t": now,
                        },
                    }
                },
            )
        return httpx.Response(
            200,
            json=order_payload(
                symbol="ETH/USD",
                qty="0.01",
                filled_qty="0",
                filled_avg_price=None,
                limit_price="2501",
                status="new",
            ),
        )

    paper = broker(handler, allow_submit=True, symbols=("BTC/USD", "ETH/USD", "SOL/USD"))
    markets = paper.get_markets(("BTC/USD", "ETH/USD", "SOL/USD"))
    assert tuple(markets) == ("BTC/USD", "ETH/USD", "SOL/USD")
    intent = OrderIntent("ETH/USD", "buy", D("0.01"), "test-client-id", D("2501"), D("25.01"), D(".062525"))
    result = paper.submit_order(intent)
    assert result.symbol == "ETH/USD"
    assert json.loads(calls[-1].content)["symbol"] == "ETH/USD"


def test_broker_refuses_supported_symbol_outside_selected_universe():
    paper = broker(
        lambda _: pytest.fail("no request expected"),
        allow_submit=True,
        symbols=("BTC/USD", "ETH/USD"),
    )
    with pytest.raises(BrokerError, match="universe"):
        paper.get_market("SOL/USD")
    with pytest.raises(OrderRejected, match="universe"):
        paper.submit_order(OrderIntent("SOL/USD", "buy", D(".01"), "client", D("100"), D("1"), D(".0025")))


@pytest.mark.parametrize(
    "book",
    [
        {},
        {"b": [], "a": [{"p": "2", "s": "1"}], "t": "2026-09-19T00:00:00Z"},
        {
            "b": [{"p": "1", "s": "0"}],
            "a": [{"p": "2", "s": "1"}],
            "t": "2026-09-19T00:00:00Z",
        },
        {
            "b": [{"p": "3", "s": "1"}],
            "a": [{"p": "2", "s": "1"}],
            "t": "2026-09-19T00:00:00Z",
        },
    ],
)
def test_bad_orderbook_fails_without_test_data_fallback(book):
    paper = broker(lambda _: httpx.Response(200, json={"orderbooks": {"BTC/USD": book}}))
    with pytest.raises(BrokerError, match="order book"):
        paper.get_market()


def test_asset_rules_require_platform_metadata():
    with pytest.raises(BrokerError, match="asset rules"):
        broker(lambda _: httpx.Response(200, json={"symbol": "BTC/USD"})).get_asset_rules()
    data = {
        "symbol": "BTCUSD",
        "class": "crypto",
        "status": "active",
        "tradable": True,
        "min_order_size": "0.00002",
        "min_trade_increment": "0.00000001",
        "price_increment": "0.01",
    }
    rules = broker(lambda _: httpx.Response(200, json=data)).get_asset_rules()
    assert rules.quantity_increment == D("0.00000001") and rules.symbol == "BTC/USD"


def test_account_uses_cash_spot_power_and_normalizes_holdings():
    account = {
        "id": "paper-account",
        "status": "ACTIVE",
        "crypto_status": "ACTIVE",
        "currency": "USD",
        "cash": "9000",
        "equity": "10000",
        "buying_power": "36000",
        "non_marginable_buying_power": "8000",
        "trading_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
    }
    positions = [{"symbol": "BTCUSD", "qty": "0.02", "qty_available": "0.01", "avg_entry_price": "49000"}]

    def handler(request):
        result = (
            account
            if request.url.path == "/v2/account"
            else positions
            if request.url.path == "/v2/positions"
            else []
        )
        return httpx.Response(200, json=result)

    paper = broker(handler)
    portfolio = paper.get_portfolio()
    assert portfolio.tradable and portfolio.buying_power_usd == D(8000)
    assert portfolio.btc_quantity == D("0.02") and portfolio.positions[0].available_quantity == D("0.01")
    account["trading_blocked"] = True
    assert not paper.get_portfolio().tradable
    positions[0]["symbol"] = "ETHUSD"
    with pytest.raises(BrokerError, match="unsupported assets"):
        paper.get_portfolio()


def test_crypto_fee_is_valued_without_inventing_a_same_day_zero_fee():
    data = [
        {
            "id": "fee1",
            "activity_type": "CFEE",
            "date": "2026-09-18",
            "net_amount": "0",
            "symbol": "BTCUSD",
            "qty": "-0.00001",
            "price": "50000",
        },
        {"id": "fee2", "activity_type": "FEE", "date": "2026-09-18", "net_amount": "-1.25"},
        {"id": "fee3", "activity_type": "CFEE", "date": "2026-09-18", "symbol": "BTCUSD", "qty": "-0.00001"},
    ]
    activities = broker(lambda _: httpx.Response(200, json=data)).get_activities()
    assert activities[0].quantity == D("-0.00001") and activities[0].fee_usd == D("0.50")
    assert activities[1].fee_usd == D("1.25") and activities[2].fee_usd is None
    assert all(activity.time_precision == "day" for activity in activities)


def test_activity_query_retains_first_day_fees_but_not_pretracking_fills():
    from crypto_agent.models import timestamp

    start = timestamp("2026-09-18T12:30:00Z")
    fill = {
        "activity_type": "FILL",
        "symbol": "BTCUSD",
        "qty": "0.01",
        "price": "50000",
        "side": "buy",
        "order_id": "order",
    }
    data = [
        fill | {"id": "old-fill", "transaction_time": "2026-09-18T11:30:00Z"},
        fill | {"id": "opening-fill", "transaction_time": start.isoformat()},
        fill | {"id": "new-fill", "transaction_time": "2026-09-18T13:30:00Z"},
        {
            "id": "same-day-fee",
            "activity_type": "CFEE",
            "date": "2026-09-18",
            "symbol": "BTCUSD",
            "qty": "-0.00001",
            "price": "50000",
        },
        {"id": "older-fee", "activity_type": "FEE", "date": "2026-09-17", "net_amount": "-1"},
    ]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=data)

    activities = broker(handler).get_activities(after=start)
    assert calls[0].url.params["after"] == "2026-09-18T00:00:00+00:00"
    assert [activity.activity_id for activity in activities] == ["new-fill", "same-day-fee"]
    assert activities[0].time_precision == "instant" and activities[1].time_precision == "day"
