"""Paper statistics use closed Tokyo buckets and audited external funding."""

from datetime import timedelta
from decimal import Decimal

import pytest

from crypto_agent.api.performance import calculate, load_performance, parse_history, read_activities
from crypto_agent.models import AgentError, timestamp

MIDNIGHT = timestamp("2026-09-20T15:00:00Z")


def history(values, flows=None):
    result = {
        "timeframe": "1Min",
        "timestamp": [int((MIDNIGHT + timedelta(minutes=i - 1)).timestamp()) for i in range(len(values))],
        "equity": values,
        "profit_loss": [-999] * len(values),
        "profit_loss_pct": [0] * len(values),
    }
    if flows is not None:
        result["cashflow"] = flows
    return result


def parsed(values, flows=None):
    return parse_history(history(values, flows), MIDNIGHT + timedelta(minutes=len(values)))


def activity(kind, amount=None):
    return {
        "id": "a",
        "activity_type": kind,
        "net_amount": amount,
        "transaction_time": (MIDNIGHT + timedelta(seconds=30)).isoformat(),
    }


def test_bucket_end_tokyo_midnight_and_unfinished_bucket():
    points = parse_history(history([100, 103, 999]), MIDNIGHT + timedelta(minutes=1, seconds=30))
    assert len(points) == 2
    assert points[0]["at"] == MIDNIGHT
    result = calculate(points, [], start=MIDNIGHT, daily=True)
    assert result["value"] == "3"
    assert result["percent"] == "0.03"
    assert "09/21 00:00" in result["subtitle"]
    assert result["source"] == "Alpaca Paper 权益历史"
    assert calculate(points[1:], [], start=MIDNIGHT, daily=True)["value"] is None


@pytest.mark.parametrize(("kind", "flow", "ending"), [("CSD", 500, 1600), ("CSW", -500, 600)])
def test_external_cash_removed_from_profit(kind, flow, ending):
    result = calculate(parsed([1000, ending], {kind: [0, flow]}), [activity(kind, str(flow))], start=MIDNIGHT)
    assert result["value"] == "100"
    assert result["percent"] == "0.1"


def test_fees_remain_in_profit_and_funding_does_not_create_drawdown():
    result = calculate(parsed([1000, 999], {"CFEE": [0, -1]}), [activity("CFEE", "-1")], start=MIDNIGHT)
    assert result["value"] == "-1"
    points = parsed([1000, 500, 450], {"CSW": [0, -500, 0]})
    result = calculate(points, [activity("CSW", "-500")], start=MIDNIGHT, drawdown=True)
    assert Decimal(result["percent"]) == Decimal("-0.1")


@pytest.mark.parametrize("kind", ["JNLC", "JNLS", "ACATS", "FOPT", "UNKNOWN"])
def test_unclassified_cash_and_even_zero_value_asset_transfers_block(kind):
    assert calculate(parsed([1000, 1100]), [activity(kind, "0")], start=MIDNIGHT)["value"] is None
    assert calculate(parsed([1000, 1100], {kind: [0, 100]}), [], start=MIDNIGHT)["value"] is None


@pytest.mark.parametrize("flows", [None, {"CSD": [0, 0]}, {"CSD": [0, 99]}])
def test_missing_or_mismatched_cashflow_cannot_turn_deposit_into_profit(flows):
    assert calculate(parsed([1000, 1100], flows), [activity("CSD", "100")], start=MIDNIGHT)["value"] is None


def test_missing_equity_gap_and_prefunding():
    assert calculate(parsed([100, None, 110]), [], start=MIDNIGHT)["value"] is None
    points = parsed([100, 101, 102])
    assert calculate(points[::2], [], start=MIDNIGHT)["value"] is None
    result = calculate(parsed([0, 1000, 1001]), [], start=MIDNIGHT)
    assert result["value"] == "1"
    assert "00:01" in result["subtitle"]


@pytest.mark.parametrize("flows", [{"CSD": [0]}, {"CSD": [0, -1]}, {"CSW": [0, 1]}, None])
def test_malformed_history_rejected(flows):
    data = history([100, 101], flows)
    if flows is None:
        data["cashflow"] = None
    with pytest.raises(AgentError):
        parse_history(data, MIDNIGHT + timedelta(minutes=2))


def test_readonly_history_parameters_and_delayed_data():
    class Broker:
        calls = []

        def _request(self, method, path, *, params):
            self.calls.append((method, path, params.copy()))
            return history([1000, 1002]) if path.endswith("history") else []

    broker = Broker()
    metrics = load_performance(broker, MIDNIGHT + timedelta(minutes=10), MIDNIGHT)
    assert all(call[0] == "GET" for call in broker.calls)
    params = broker.calls[0][2]
    assert params["start"] == (MIDNIGHT - timedelta(minutes=1)).isoformat()
    assert params["cashflow_types"] == "ALL"
    assert params["intraday_reporting"] == "continuous"
    assert params["pnl_reset"] == "no_reset"
    assert metrics["daily"]["value"] == "2"
    assert metrics["daily"]["stale"] is True


def test_activity_pagination_and_repeated_page_rejected():
    class Broker:
        calls = 0

        def _request(self, method, path, *, params):
            self.calls += 1
            if self.calls == 1:
                return [{"id": str(i)} for i in range(100)]
            assert params["page_token"] == "99"
            return [{"id": "0"}]

    with pytest.raises(AgentError, match="分页不完整"):
        read_activities(Broker(), MIDNIGHT, MIDNIGHT + timedelta(minutes=1))


def test_wide_audit_catches_date_only_asset_transfer_but_not_old_cash_seed():
    class Broker:
        reads = 0

        def _request(self, method, path, *, params):
            if path.endswith("history"):
                return history([1000, 1002])
            self.reads += 1
            if self.reads % 2:
                return []
            return [{"id": "transfer", "activity_type": self.kind, "date": "2026-09-20", "net_amount": "0"}]

    broker = Broker()
    broker.kind = "JNLS"
    result = load_performance(broker, MIDNIGHT + timedelta(minutes=2), MIDNIGHT)
    assert result["daily"]["value"] is None
    broker.kind = "JNLC"
    result = load_performance(broker, MIDNIGHT + timedelta(minutes=2), MIDNIGHT)
    assert result["daily"]["value"] == "2"


def test_nonpositive_flow_adjusted_equity_cannot_produce_drawdown_below_100_percent():
    points = parsed([1000, 100], {"CSD": [0, 200]})
    result = calculate(points, [activity("CSD", "200")], start=MIDNIGHT, drawdown=True)
    assert result["percent"] is None


def test_statistics_failure_retains_values_and_tokyo_midnight_invalidates_cache(monkeypatch):
    from crypto_agent.api.service import Monitor

    now = [timestamp("2026-09-20T14:59:30Z")]
    monkeypatch.setattr("crypto_agent.api.service.utcnow", lambda: now[0])
    monitor = Monitor(demo_mode=True)
    monitor.demo_mode = False
    for name in ("account", "ledger", "agent", "equity"):
        setattr(monitor, name, lambda: ({}, now[0].isoformat(), "", False))
    calls = []

    def load():
        calls.append(now[0])
        return {"daily": {"value": "1", "percent": "0.01", "basis": "tested"}}, now[0].isoformat(), "", False

    monitor.performance = load
    monitor.dashboard()
    assert len(calls) == 1
    monitor.dashboard()
    assert len(calls) == 1
    now[0] += timedelta(seconds=31)
    monitor.dashboard()
    assert len(calls) == 2

    def fail():
        raise AgentError("history unavailable")

    monitor.performance = fail
    attempted, value = monitor.cache.entries["performance"]
    monitor.cache.entries["performance"] = (attempted - 61, value)
    sections = monitor.dashboard()["sections"]
    assert sections["account"]["stale"] is False
    metric = sections["account"]["data"]["metrics"]["daily"]
    assert metric["value"] == "1"
    assert metric["stale"] is True
    assert metric["error"] == "history unavailable"


def test_long_history_is_split_without_losing_or_double_counting_boundary_funding():
    start = MIDNIGHT
    now = start + timedelta(days=7, minutes=4)
    deposit_at = start + timedelta(days=6)

    class Broker:
        history_windows = []

        def _request(self, method, path, *, params):
            assert method == "GET"
            if path.endswith("activities"):
                return [
                    {
                        "id": "deposit",
                        "activity_type": "CSD",
                        "net_amount": "500",
                        "transaction_time": deposit_at.isoformat(),
                    }
                ]
            begin, end = timestamp(params["start"]), timestamp(params["end"])
            self.history_windows.append((begin, end))
            assert end - begin <= timedelta(days=6)
            # Provider includes the bucket at `end`; it belongs to the next request.
            times = [begin + timedelta(minutes=i) for i in range(int((end - begin).total_seconds() / 60) + 1)]
            return {
                "timeframe": "1Min",
                "timestamp": [int(t.timestamp()) for t in times],
                "equity": [1500 if t + timedelta(minutes=1) >= deposit_at else 1000 for t in times],
                "cashflow": {"CSD": [500 if t + timedelta(minutes=1) == deposit_at else 0 for t in times]},
            }

    broker = Broker()
    result = load_performance(broker, now, start)
    assert len(broker.history_windows) == 2
    assert broker.history_windows[0][1] == broker.history_windows[1][0]
    assert result["total"]["value"] == "0"
    assert result["total"]["closing_equity"] == "1500"
    assert result["drawdown"]["percent"] == "0"
    assert result["daily"]["value"] == "0"
    assert result["total"]["as_of"] == now.isoformat()


def test_balanced_unclassified_journals_are_not_assumed_to_be_reversals():
    # +100 could be a deposit while -100 could be a fee: net-zero journals
    # do not establish zero external funding or a reliable sampled drawdown.
    points = parsed([1000, 1100, 1000], {"JNLC": [0, 100, -100]})
    for drawdown in (False, True):
        result = calculate(points, [], start=MIDNIGHT, drawdown=drawdown)
        assert result["value"] is None and result["percent"] is None
        assert "2 笔未核对资金调整" in result["subtitle"]
        assert "JNLC +100 USD" in result["basis"]
        assert "JNLC -100 USD" in result["basis"]
