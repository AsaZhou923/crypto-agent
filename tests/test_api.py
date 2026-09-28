"""Monitor invariants: provenance, read-only isolation, finance and cache behavior."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from crypto_agent.api import demo
from crypto_agent.api.app import create_app
from crypto_agent.api.database import agent_view, equity_view, read_database
from crypto_agent.api.metrics import max_drawdown, performance, tokyo_day_start
from crypto_agent.api.service import Cache, Monitor
from crypto_agent.brokers.alpaca_paper import PAPER_URL, AlpacaPaperBroker
from crypto_agent.models import AgentError, timestamp


def client(**kwargs):
    return TestClient(create_app(**kwargs), base_url="http://127.0.0.1")


def test_demo_consistent_ledger_and_account():
    data = demo.dashboard()
    account = data["sections"]["account"]["data"]
    ledger = data["sections"]["ledger"]["data"]
    cash, quantity = Decimal(100000), Decimal(0)
    for fill in ledger["fills"]:
        sign = 1 if fill["side"] == "buy" else -1
        cash -= sign * Decimal(fill["quantity"]) * Decimal(fill["price"]) + Decimal(fill["fee"])
        quantity += sign * Decimal(fill["quantity"])
    assert Decimal(account["cash"]) == cash
    assert Decimal(account["positions"][0]["quantity"]) == quantity
    assert Decimal(account["equity"]) == cash + quantity * Decimal(account["positions"][0]["current_price"])
    assert Decimal(account["metrics"]["total"]["value"]) == Decimal(account["equity"]) - 100000
    assert demo.dashboard() == data
    assert ledger["orders"][0]["status"] == "partially_filled"
    assert Decimal(ledger["orders"][0]["quantity"]) > Decimal(ledger["orders"][0]["filled_quantity"])
    assert data["sections"]["agent"]["data"]["decisions"][0]["target_position_pct"] is None
    assert any(not r["allowed"] for d in data["sections"]["agent"]["data"]["decisions"] for r in d["risk"])
    assert data["sections"]["equity"]["data"]["points"][-1]["value"] == "100260.246000"


def test_cashflow_adjustment_and_insufficient_evidence():
    result = performance("1000", "1600", "500", complete=True, basis="opening capital")
    assert result["value"] == "100"
    assert result["percent"] == "0.1"
    assert performance("1000", "600", "-500", complete=True, basis="") == {
        "value": "100",
        "percent": "0.1",
        "basis": "",
    }
    assert performance("1000", "1600", None, complete=False, basis="")["value"] is None
    assert performance("0", "1600", "500", complete=True, basis="")["percent"] is None
    drawdown = max_drawdown(
        [{"value": "1000"}, {"value": "2000", "net_flow": "1000"}, {"value": "1800"}],
        flows_complete=True,
        basis="",
    )
    assert Decimal(drawdown["percent"]) == Decimal("-.1")
    assert max_drawdown([{"value": "1000"}], flows_complete=True, basis="")["percent"] is None
    assert (
        max_drawdown([{"value": "1000"}, {"value": "900"}], flows_complete=False, basis="")["percent"] is None
    )


def test_tokyo_boundary_is_not_utc_midnight():
    assert tokyo_day_start("2026-09-18T15:00:00Z").isoformat() == "2026-09-19T00:00:00+09:00"
    assert tokyo_day_start("2026-09-18T14:59:59Z").isoformat() == "2026-09-18T00:00:00+09:00"
    with pytest.raises(AgentError):
        tokyo_day_start("2026-09-19T00:00:00")


def test_demo_api_scenarios_sources_and_readonly(tmp_path):
    with client(demo_mode=True, root=tmp_path) as c:
        assert c.get("/api/dashboard").json()["mode"] == "demo"
        assert c.get("/api/dashboard?scenario=empty").json()["sections"]["account"]["data"] is None
        partial = c.get("/api/dashboard?scenario=partial").json()["sections"]
        assert partial["ledger"]["error"] and partial["account"]["error"] is None
        assert c.get("/api/market?scenario=stale").json()["stale"]
        assert c.get("/api/market?timeframe=1Hour").json()["data"]["timeframe"] == "1Hour"
        assert c.get("/api/market?timeframe=1Day").status_code == 422
        assert c.get("/api/unknown").status_code == 404
        assert c.post("/api/orders", json={"side": "buy"}).status_code == 405
        assert c.get("/api/dashboard", headers={"Host": "evil.example"}).status_code == 403
        assert c.get("/api/dashboard", headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.get("/api/dashboard", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert not list(tmp_path.iterdir())


def test_paper_failure_never_becomes_demo(tmp_path):
    with client(root=tmp_path) as c:
        data = c.get("/api/dashboard").json()
        assert data["mode"] == "paper"
        assert data["sections"]["account"]["data"] is None
        assert data["sections"]["account"]["error"]
        assert c.get("/api/dashboard?scenario=empty").status_code == 400


def test_response_redacts_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret-to-redact-123456")
    monitor = Monitor(demo_mode=True, root=tmp_path)
    monitor.dashboard = lambda **kwargs: {"reason": "saved text secret-to-redact-123456"}
    with client(monitor=monitor) as c:
        response = c.get("/api/dashboard")
        assert "secret-to-redact-123456" not in response.text
        assert "[REDACTED]" in response.text


def test_cache_singleflight_and_stale_retention():
    cache = Cache()
    calls = []
    barrier = threading.Barrier(6)

    def loader():
        calls.append(1)
        time.sleep(0.03)
        return {"equity": "1000"}, "2026-09-19T12:00:00Z", "test", False

    def invoke():
        barrier.wait()
        return cache.get("account", loader, source="Alpaca Paper", ttl=30, refresh=True)

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: invoke(), range(6)))
    assert len(calls) == 1
    assert all(result == responses[0] for result in responses)
    cache.entries["account"] = (time.monotonic() - 40, responses[0])

    def fail():
        raise AgentError("connection failed")

    result = cache.get("account", fail, source="Alpaca Paper", ttl=30)
    assert result["data"] == {"equity": "1000"}
    assert result["stale"] and result["error"] == "connection failed"
    assert result["as_of"] == "2026-09-19T12:00:00Z"


def test_readonly_database_missing_and_account_binding(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(AgentError, match="尚未建立"):
        read_database(path)
    assert not path.exists()
    import sqlite3

    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.executemany("INSERT INTO metadata VALUES (?,?)", [("mode", "paper"), ("account_id", "account-a")])
    before = path.read_bytes()
    data = read_database(path)
    assert path.read_bytes() == before
    assert equity_view(data, "account-b")["points"] == []
    assert "不匹配" in agent_view(data, "account-b")["notice"]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE metadata SET value='offline' WHERE key='mode'")
    with pytest.raises(AgentError, match="不是 Paper"):
        read_database(path)


def test_paper_queries_only_fixed_origins_get_and_preserve_states():
    requests = []
    order = {
        "id": "order-1",
        "client_order_id": "client-1",
        "symbol": "BTCUSD",
        "side": "buy",
        "qty": ".05",
        "filled_qty": ".02",
        "filled_avg_price": "65000",
        "status": "partially_filled",
        "limit_price": "65000",
        "created_at": "2026-09-19T10:00:00Z",
        "submitted_at": "2026-09-19T10:00:01Z",
        "updated_at": "2026-09-19T10:05:00Z",
    }

    def respond(request):
        requests.append(request)
        if request.url.path == "/v2/orders":
            return httpx.Response(200, json=[order])
        if request.url.path == "/v2/account/activities":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/bars"):
            return httpx.Response(
                200,
                json={
                    "bars": {
                        "BTC/USD": [
                            {
                                "t": "2026-09-19T12:00:00Z",
                                "o": 65000,
                                "h": 65002,
                                "l": 64999,
                                "c": 65001,
                                "v": 1,
                            }
                        ]
                    }
                },
            )
        raise AssertionError(request.url)

    broker = AlpacaPaperBroker(PAPER_URL, "key", "secret", transport=httpx.MockTransport(respond))
    monitor = Monitor(config_dir=Path("config/demo"), broker=broker)
    ledger = monitor.ledger()[0]
    assert ledger["orders"][0]["submitted_at"] == "2026-09-19T10:00:01Z"
    assert ledger["orders"][0]["status"] == "partially_filled"
    assert ledger["orders"][0]["run_id"] is None
    assert monitor.market("5Min")["data"]["timeframe"] == "5Min"
    assert all(r.method == "GET" for r in requests)
    assert all(r.url.host in {"paper-api.alpaca.markets", "data.alpaca.markets"} for r in requests)
    assert not broker.allow_submit
    broker.close()


def test_cycles_have_real_duration_safe_messages_and_latest_status(tmp_path):
    import sqlite3

    path = tmp_path / "cycles.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
        db.executemany("INSERT INTO metadata VALUES (?,?)", [("mode", "paper"), ("account_id", "a")])
        db.execute(
            "CREATE TABLE auto_cycles(id TEXT,started_at TEXT,ended_at TEXT,status TEXT,run_id TEXT,body TEXT)"
        )
        db.executemany(
            "INSERT INTO auto_cycles VALUES (?,?,?,?,?,?)",
            [
                (
                    "completed",
                    "2026-09-19T11:58:00Z",
                    "2026-09-19T12:00:01.250Z",
                    "failed",
                    "run-old",
                    json.dumps(
                        {
                            "reason": "Model timeout",
                            "warnings": ["Delayed quote", {"config": "hidden"}],
                            "config": {"secret": "must-not-be-exposed"},
                            "automatic_paused": True,
                        }
                    ),
                ),
                ("open", "2026-09-19T11:59:00Z", None, "started", "run-new", None),
            ],
        )
    data = read_database(path)
    assert len(data["cycles"]) == 2
    data["runs"] = [
        {"id": "run-new", "created_at": "2026-09-19T11:59:10Z", "status": "analyzed", "error": None}
    ]
    result = agent_view(data, "a")
    cycles = {row["id"]: row for row in result["logs"] if row["phase"] == "auto_cycle"}
    assert cycles["cycle-completed"]["duration_ms"] == 121250
    assert cycles["cycle-open"]["duration_ms"] is None
    assert result["status"] == "failed"
    assert result["last_run_at"] == "2026-09-19T12:00:01.250000+00:00"
    assert "Model timeout" in cycles["cycle-completed"]["error"]
    assert "Delayed quote" in cycles["cycle-completed"]["error"]
    assert "must-not-be-exposed" not in json.dumps(result)
    assert "hidden" not in json.dumps(result)


def test_agent_observation_uses_saved_run_time_and_stale_threshold(tmp_path, monkeypatch):
    from crypto_agent.models import timestamp

    monitor = Monitor(root=tmp_path)
    data = {
        "metadata": {},
        "runs": [],
        "decisions": [],
        "orders": [],
        "risk": [],
        "snapshots": [],
        "cycles": [
            {
                "id": "cycle",
                "started_at": "2026-09-19T11:59:00Z",
                "ended_at": "2026-09-19T12:00:00Z",
                "status": "failed",
                "run_id": None,
                "body": json.dumps({"reason": "No market data"}),
            }
        ],
    }
    monitor.database = lambda: data
    monkeypatch.setattr("crypto_agent.api.service.utcnow", lambda: timestamp("2026-09-19T12:30:01Z"))
    value, observed, notice, stale = monitor.agent()
    assert observed == "2026-09-19T12:00:00+00:00"
    assert stale and "30 分钟" in notice
    assert value["status"] == "failed"
    monkeypatch.setattr("crypto_agent.api.service.utcnow", lambda: timestamp("2026-09-19T12:30:00Z"))
    assert monitor.agent()[3] is False


def test_corrupt_order_association_does_not_hide_platform_ledger():
    def respond(request):
        if request.url.path == "/v2/orders":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "platform-order",
                        "client_order_id": "local-client",
                        "symbol": "BTCUSD",
                        "side": "buy",
                        "qty": ".02",
                        "filled_qty": ".02",
                        "filled_avg_price": "65000",
                        "status": "filled",
                        "created_at": "2026-09-19T10:00:00Z",
                        "submitted_at": "2026-09-19T10:00:01Z",
                    }
                ],
            )
        if request.url.path == "/v2/account/activities":
            return httpx.Response(200, json=[])
        raise AssertionError(request.url)

    broker = AlpacaPaperBroker(PAPER_URL, "key", "secret", transport=httpx.MockTransport(respond))
    monitor = Monitor(config_dir=Path("config/demo"), broker=broker)
    monitor.account_id = "account-a"
    monitor.database = lambda: {
        "metadata": {"account_id": "account-a"},
        "orders": [{"client_order_id": "local-client", "run_id": "run-1", "broker_json": "not-valid-json"}],
    }
    result = monitor.ledger()[0]
    assert result["orders"][0]["id"] == "platform-order"
    assert result["orders"][0]["run_id"] is None
    assert "关联记录损坏" in result["notice"]
    broker.close()


def test_demo_stale_preserves_observation_and_advances_clock():
    normal = demo.dashboard()
    stale = demo.scenario_response(normal, "stale")
    assert timestamp(stale["as_of"]) > timestamp(normal["as_of"])
    for name, section in stale["sections"].items():
        assert section["as_of"] == normal["sections"][name]["as_of"]
        assert section["data"] == normal["sections"][name]["data"]
    decision = stale["sections"]["agent"]["data"]["decisions"][0]
    assert timestamp(decision["expires_at"]) < timestamp(stale["as_of"])


def test_equity_refreshes_from_actual_account_without_waiting_for_agent(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from crypto_agent.api import service

    clock = timestamp("2026-09-19T12:00:00Z")
    portfolio = SimpleNamespace(
        account_id="paper-a",
        observed_at=clock,
        equity_usd=Decimal("1001"),
        cash_usd=Decimal("1001"),
        buying_power_usd=Decimal("1001"),
    )
    broker = SimpleNamespace(get_portfolio=lambda: portfolio, _request=lambda *a: [])
    monitor = Monitor(demo_mode=True, root=tmp_path, broker=broker)
    monkeypatch.setattr(service, "utcnow", lambda: clock)
    monkeypatch.setattr(
        monitor,
        "database",
        lambda: {
            "metadata": {"account_id": "paper-a"},
            "snapshots": [
                {
                    "observed_at": "2026-09-19T10:00:00+00:00",
                    "portfolio_json": json.dumps({"account_id": "paper-a", "equity_usd": "1000"}),
                }
            ],
        },
    )
    monitor.account()
    value, observed, _, stale = monitor.equity()
    assert not stale
    assert value["points"][-1] == {"time": clock.isoformat(), "value": "1001"}
    assert len(value["points"]) == 2
    assert timestamp(observed) == clock
    clock = timestamp("2026-09-19T12:00:30Z")
    portfolio.observed_at, portfolio.equity_usd = clock, Decimal("1002")
    monitor.account()
    assert monitor.equity()[0]["points"][-1]["value"] == "1002"
    # Neither clock ticks nor a failed network read may fabricate an observation.
    clock = timestamp("2026-09-19T12:03:00Z")
    assert monitor.equity()[3]
    assert len(monitor.equity()[0]["points"]) == 3
    assert not list(tmp_path.iterdir())
    # A different account must not inherit either local or in-memory history.
    portfolio.account_id = "paper-b"
    portfolio.observed_at = clock
    monitor.account()
    assert len(monitor.equity()[0]["points"]) == 1
