"""Market identity, read-only source isolation and per-market stale retention."""

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from crypto_agent.api import demo
from crypto_agent.api.app import create_app
from crypto_agent.api.service import Monitor
from crypto_agent.brokers.alpaca_paper import PAPER_URL, AlpacaPaperBroker
from crypto_agent.models import AgentError


def test_market_symbol_api_validation_and_btc_default(tmp_path):
    with TestClient(create_app(demo_mode=True, root=tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/api/market").json()["data"]["symbol"] == "BTC/USD"
        response = client.get("/api/market", params={"symbol": "XRP/USD", "timeframe": "5Min"})
        assert response.status_code == 200
        assert response.json()["data"]["symbol"] == "XRP/USD"
        assert response.json()["data"]["timeframe"] == "5Min"
        assert response.json()["source"] == "离线演示"
        for symbol in ["BTCUSD", "ETH/USD", "XRP/USD,BTC/USD", ""]:
            assert client.get("/api/market", params={"symbol": symbol}).status_code == 422
        assert client.get("/api/market", params={"symbol": "XRP/USD", "timeframe": "1Day"}).status_code == 422


@pytest.mark.parametrize("demo_mode", [False, True])
def test_market_internal_validation_precedes_requests(tmp_path, demo_mode):
    monitor = Monitor(root=tmp_path, demo_mode=demo_mode)
    with pytest.raises(AgentError, match="仅支持"):
        monitor.market(symbol="ETH/USD")
    with pytest.raises(AgentError, match="周期"):
        monitor.market(timeframe="1Day", symbol="XRP/USD")
    assert monitor.cache.entries == {}


def test_xrp_demo_is_repeatable_independent_and_does_not_invent_trades():
    btc, xrp = demo.market(), demo.market(symbol="XRP/USD")
    assert xrp == demo.market(symbol="XRP/USD")
    assert len(xrp["data"]["bars"]) == 180
    assert btc["data"]["bars"] != xrp["data"]["bars"]
    assert "演示" in xrp["data"]["notice"]
    assert "没有 XRP 演示成交或决策" in xrp["data"]["notice"]
    for bar in xrp["data"]["bars"]:
        assert Decimal("0") < Decimal(bar["low"]) <= min(Decimal(bar["open"]), Decimal(bar["close"]))
        assert Decimal(bar["high"]) >= max(Decimal(bar["open"]), Decimal(bar["close"]))
        assert Decimal(bar["volume"]) > 0
    sections = demo.dashboard()["sections"]
    for rows in [sections["ledger"]["data"]["fills"], sections["agent"]["data"]["decisions"]]:
        assert all(row["symbol"] == "BTC/USD" for row in rows)


def test_market_cache_and_failed_refresh_are_isolated_by_symbol_and_timeframe():
    calls, failed = [], set()

    def respond(request):
        key = (request.url.params["symbols"], request.url.params["timeframe"])
        calls.append(key)
        assert request.method == "GET"
        assert request.url.host == "data.alpaca.markets"
        assert request.url.path == "/v1beta3/crypto/us/bars"
        if key in failed:
            raise httpx.ConnectError("Offline", request=request)
        # Both keys are returned deliberately: selection must follow the requested symbol.
        return httpx.Response(
            200,
            json={
                "bars": {
                    symbol: [
                        {
                            "t": "2026-09-21T10:00:00Z",
                            "o": price,
                            "h": price,
                            "l": price,
                            "c": price,
                            "v": "3",
                        }
                    ]
                    for symbol, price in [("BTC/USD", "65000"), ("XRP/USD", ".61")]
                }
            },
        )

    broker = AlpacaPaperBroker(PAPER_URL, "test-key", "test-secret", transport=httpx.MockTransport(respond))
    monitor = Monitor(config_dir=Path("config/demo"), broker=broker)
    try:
        btc = monitor.market()
        xrp = monitor.market(symbol="XRP/USD")
        xrp_hour = monitor.market("1Hour", symbol="XRP/USD")
        assert btc["data"]["bars"][0]["close"] == "65000"
        assert xrp["data"]["bars"][0]["close"] == "0.61"
        assert xrp_hour["data"]["timeframe"] == "1Hour"
        assert xrp["source"] == "Alpaca Crypto US"
        assert monitor.market() == btc
        assert monitor.market(symbol="XRP/USD") == xrp
        assert len(calls) == 3

        failed.add(("XRP/USD", "1Min"))
        cache_key = "market-XRP/USD-1Min"
        monitor.cache.entries[cache_key] = (time.monotonic() - 61, xrp)
        stale = monitor.market(symbol="XRP/USD", refresh=True)
        assert stale["stale"] and stale["error"]
        assert stale["data"] == xrp["data"]
        assert stale["as_of"] == xrp["as_of"]
        assert monitor.market() == btc
        assert monitor.market("1Hour", symbol="XRP/USD") == xrp_hour

        failed.add(("XRP/USD", "5Min"))
        missing = monitor.market("5Min", symbol="XRP/USD")
        assert missing["data"] is None and missing["error"]
        assert missing["source"] == "Alpaca Crypto US"
        assert not broker.allow_submit
    finally:
        monitor.close()


def test_market_view_follows_short_bar_page_next_token():
    calls = []
    start = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)

    def rows(offset, count):
        return [
            {
                "t": (start - timedelta(minutes=offset + index + 1)).isoformat(),
                "o": "65000",
                "h": "65002",
                "l": "64999",
                "c": "65001",
                "v": "0",
            }
            for index in range(count)
        ]

    def respond(request):
        calls.append(request)
        token = request.url.params.get("page_token")
        if token is None:
            return httpx.Response(200, json={"bars": {"BTC/USD": rows(0, 90)}, "next_page_token": "older"})
        assert token == "older"
        return httpx.Response(200, json={"bars": {"BTC/USD": rows(90, 90)}, "next_page_token": None})

    broker = AlpacaPaperBroker(PAPER_URL, "test-key", "test-secret", transport=httpx.MockTransport(respond))
    monitor = Monitor(config_dir=Path("config/demo"), broker=broker)
    try:
        result = monitor.market(refresh=True)
        assert result["error"] is None
        assert len(result["data"]["bars"]) == 180
        assert len(calls) == 2
        assert calls[1].url.params["page_token"] == "older"
    finally:
        monitor.close()


def test_xrp_demo_scenarios_preserve_selected_market(tmp_path):
    monitor = Monitor(demo_mode=True, root=tmp_path)
    stale = monitor.market(symbol="XRP/USD", scenario="stale")
    assert stale["stale"] and stale["data"]["symbol"] == "XRP/USD"
    assert monitor.market(symbol="XRP/USD", scenario="empty")["data"] is None


def test_successful_but_delayed_bars_keep_distinct_query_and_data_times(monkeypatch):
    from crypto_agent.models import timestamp

    now = timestamp("2026-09-26T07:40:00Z")
    monkeypatch.setattr("crypto_agent.api.service.utcnow", lambda: now)

    class Broker:
        def _request(self, method, path, **kwargs):
            assert method == "GET"
            return {
                "bars": {
                    "BTC/USD": [{"t": "2026-09-26T07:25:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 2}]
                }
            }

    monitor = Monitor(demo_mode=True, broker=Broker())
    monitor.demo_mode = False
    response = monitor.market("5Min")
    assert response["error"] is None and response["stale"] is True
    assert timestamp(response["as_of"]) == timestamp("2026-09-26T07:25:00Z")
    assert response["data"]["checked_at"] == now.isoformat()
    assert "查询成功" in response["notice"] and "不补造" in response["notice"]
