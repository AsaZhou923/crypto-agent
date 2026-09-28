"""Scheduled digest tests with a local audit log and no external webhook."""

import importlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
def digest(monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("paper_digest")


def event(at, kind, message=None):
    value = {"at": at.isoformat(), "kind": kind}
    if message is not None:
        value["message"] = message
    return value


def test_tokyo_slots_include_overnight_window(digest):
    morning = datetime(2026, 9, 29, 9, 0, tzinfo=JST).astimezone(UTC)
    assert digest.slot_at(morning + timedelta(seconds=2)) == morning
    assert digest.previous_slot(morning).astimezone(JST).hour == 21
    assert digest.previous_slot(morning).astimezone(JST).day == 28


def test_digest_groups_fills_and_other_trade_events(digest):
    end = datetime(2026, 9, 28, 21, 0, tzinfo=JST).astimezone(UTC)
    start = digest.previous_slot(end)
    events = [
        event(end - timedelta(minutes=10), "status"),
        event(end - timedelta(minutes=9), "event", "BTC/USD buy 成交增加 0.001；状态 partially_filled"),
        event(end - timedelta(minutes=8), "event", "BTC/USD buy 成交增加 0.002；状态 filled"),
        event(end - timedelta(minutes=7), "event", "XRP/USD sell 成交增加 12；状态 filled"),
        event(end - timedelta(minutes=6), "event", "订单拒绝：ca-1"),
        event(end - timedelta(minutes=5), "event", "BTC/USD 参数倍数调整为 0.75"),
    ]
    message = digest.message_for(events, start, end)
    assert "BTC/USD 买入 0.003" in message
    assert "XRP/USD 卖出 12" in message
    assert "订单拒绝：1 次" in message
    assert "参数倍数调整为 0.75" in message
    assert "调度记录已过期" not in message


def test_only_one_notification_per_slot_even_if_restarted(digest, tmp_path, monkeypatch):
    end = datetime(2026, 9, 28, 21, 0, tzinfo=JST).astimezone(UTC)
    directory = tmp_path / "runtime/local-scheduler"
    directory.mkdir(parents=True)
    entries = [
        event(end - timedelta(minutes=15), "status"),
        event(end - timedelta(minutes=10), "event", "BTC/USD buy 成交增加 0.001；状态 filled"),
        event(end, "event", "XRP/USD sell 成交增加 2；状态 filled"),
    ]
    (directory / "audit.jsonl").write_text("\n".join(json.dumps(item) for item in entries) + "\n")
    notify = Mock(return_value=True)
    monkeypatch.setattr(digest.Scheduler, "notify", notify)

    assert digest.run(tmp_path, end + timedelta(seconds=1)) == 0
    assert digest.run(tmp_path, end + timedelta(seconds=2)) == 0
    notify.assert_called_once()
    assert "BTC/USD 买入 0.001" in notify.call_args.args[0]
    assert "XRP/USD" not in notify.call_args.args[0]
    assert json.loads((directory / "digest-state.json").read_text())["last_slot"] == end.isoformat()


def test_failed_delivery_does_not_retry_or_duplicate(digest, tmp_path, monkeypatch):
    end = datetime(2026, 9, 28, 21, 0, tzinfo=JST).astimezone(UTC)
    directory = tmp_path / "runtime/local-scheduler"
    directory.mkdir(parents=True)
    (directory / "audit.jsonl").write_text("")
    notify = Mock(return_value=False)
    monkeypatch.setattr(digest.Scheduler, "notify", notify)

    assert digest.run(tmp_path, end) == 1
    assert digest.run(tmp_path, end + timedelta(seconds=1)) == 0
    notify.assert_called_once()
