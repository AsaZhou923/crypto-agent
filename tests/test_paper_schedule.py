"""Scheduler boundary tests; no broker, model, or live order submission."""

import fcntl
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "paper_schedule", Path(__file__).resolve().parents[1] / "scripts/paper_schedule.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(module.Scheduler, "notify", Mock())
    instance = module.Scheduler(tmp_path)
    instance.config.mkdir(parents=True)
    instance.settings = Mock(return_value=SimpleNamespace(digest="approved"))
    instance.report_changes = Mock()
    module.save_json(instance.policy, {"config_digest": "approved"})
    return instance


def enabled():
    return {"enabled": True, "state": {"approval_digest": "approved"}}


def test_paused_is_status_only(scheduler):
    scheduler.invoke = Mock(return_value=(0, {"enabled": False}))
    assert scheduler.run() == 0
    scheduler.invoke.assert_called_once_with("auto-status")
    scheduler.settings.assert_not_called()
    assert not scheduler.inflight.exists()
    scheduler.notify.assert_not_called()


@pytest.mark.parametrize(
    "outcome",
    [
        "blocked",
        "no_order",
        "busy",
        "cooldown",
        "pending",
        "partially_filled",
        "rate_limited",
        "read_unavailable",
    ],
)
def test_normal_skips_never_pause_or_retry(scheduler, outcome):
    scheduler.invoke = Mock(
        side_effect=[
            (0, enabled()),
            (2 if outcome == "blocked" else 0, {"status": outcome, "results": [{"status": outcome}]}),
        ]
    )
    assert scheduler.run() == 0
    assert [c.args[0] for c in scheduler.invoke.call_args_list] == ["auto-status", "auto-tick"]
    assert not scheduler.pause.exists()
    assert not scheduler.inflight.exists()
    scheduler.notify.assert_not_called()


@pytest.mark.parametrize("outcome", ["failed", "unknown", "submitting", "halted"])
def test_real_fault_stops_even_after_first_coin_fill(scheduler, outcome):
    scheduler.invoke = Mock(
        side_effect=[
            (0, enabled()),
            (2, {"status": outcome, "results": [{"status": "filled"}, {"status": outcome}]}),
        ]
    )
    assert scheduler.run() == 1
    assert scheduler.pause.exists()
    assert scheduler.invoke.call_count == 2
    scheduler.notify.assert_called_once()


def test_automatic_pause_is_preserved_on_ordinary_block(scheduler):
    scheduler.invoke = Mock(
        side_effect=[(0, enabled()), (2, {"status": "blocked", "automatic_paused": True})]
    )
    assert scheduler.run() == 1
    assert scheduler.pause.exists()


def test_interrupted_tick_is_not_replayed(scheduler):
    module.save_json(scheduler.inflight, {"started_at": "past"})
    scheduler.invoke = Mock(return_value=(0, enabled()))
    assert scheduler.run() == 1
    assert scheduler.pause.exists()
    assert scheduler.inflight.exists()
    scheduler.invoke.assert_called_once_with("auto-status")


def test_bad_json_after_possible_submission_keeps_latch(scheduler):
    def invoke(command):
        if command == "auto-status":
            return 0, enabled()
        assert scheduler.inflight.exists()
        raise ValueError("secret in third party error")

    scheduler.invoke = Mock(side_effect=invoke)
    assert scheduler.run() == 1
    assert scheduler.pause.exists()
    assert scheduler.inflight.exists()
    assert scheduler.invoke.call_count == 2
    assert "secret in third party error" not in (scheduler.directory / "audit.jsonl").read_text()


def test_cadence_checked_before_tick(scheduler):
    state = enabled()
    state["state"]["last_started_at"] = datetime.now(UTC).isoformat()
    scheduler.invoke = Mock(return_value=(0, state))
    assert scheduler.run() == 0
    scheduler.invoke.assert_called_once_with("auto-status")


def test_external_lock_prevents_overlap(scheduler):
    scheduler.invoke = Mock()
    with (scheduler.directory / "scheduler.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert scheduler.run() == 0
    scheduler.invoke.assert_not_called()


def test_policy_drift_stops_before_tick(scheduler):
    scheduler.settings.return_value.digest = "changed"
    scheduler.invoke = Mock(return_value=(0, enabled()))
    assert scheduler.run() == 1
    scheduler.invoke.assert_called_once_with("auto-status")
    assert scheduler.pause.exists()


def test_check_only_never_submits_even_when_enabled(scheduler):
    scheduler.invoke = Mock(return_value=(0, enabled()))
    assert scheduler.run(check_only=True) == 0
    scheduler.invoke.assert_called_once_with("auto-status")


def test_unknown_output_fails_closed(scheduler):
    scheduler.invoke = Mock(side_effect=[(0, enabled()), (0, {"status": "new_unsupported_state"})])
    assert scheduler.run() == 1
    assert scheduler.pause.exists()
    assert scheduler.inflight.exists()


def test_timeout_pauses_before_process_group_kill(scheduler, monkeypatch):
    process = Mock(pid=123, returncode=-9)
    process.communicate.side_effect = [subprocess.TimeoutExpired("cli", 900), ("", "")]
    context = Mock()
    context.__enter__ = Mock(return_value=process)
    context.__exit__ = Mock(return_value=False)
    popen = Mock(return_value=context)
    monkeypatch.setattr(module.subprocess, "Popen", popen)

    def kill(pid, sig):
        assert scheduler.pause.exists()
        assert pid == 123

    monkeypatch.setattr(module.os, "killpg", kill)
    with pytest.raises(RuntimeError):
        scheduler.invoke("auto-tick")
    args = popen.call_args.args[0]
    assert args[-2:] == ["auto-tick", "--execute-paper"]
    assert "--db" not in args
    assert args[args.index("--mode") + 1] == "paper"
    assert popen.call_count == 1


def test_notifications_only_new_fill_or_rejection(scheduler):
    scheduler.orders = Mock(
        return_value={
            "ca-1": {
                "status": "partially_filled",
                "symbol": "BTC/USD",
                "side": "buy",
                "filled_quantity": "0.001",
            }
        }
    )
    module.Scheduler.report_changes(scheduler, {"status": "pending"})
    assert scheduler.notify.call_count == 1
    module.Scheduler.report_changes(scheduler, {"status": "pending"})
    assert scheduler.notify.call_count == 1
    scheduler.orders.return_value["ca-1"].update(status="filled", filled_quantity="0.002")
    module.Scheduler.report_changes(scheduler, {"status": "no_order"})
    assert scheduler.notify.call_count == 2
    assert "0.001" in scheduler.notify.call_args.args[0]
    assert json.loads((scheduler.directory / "seen-orders.json").read_text())["ca-1"]["status"] == "filled"


def test_plist_never_enables_or_auto_restarts(tmp_path):
    plist = module.launch_agent(tmp_path)
    assert plist["StartInterval"] == 300
    assert plist["KeepAlive"] is False
    assert plist["RunAtLoad"] is False
    assert plist["WorkingDirectory"] == str(tmp_path)
    assert "auto-enable" not in str(plist)
    assert "Codex" not in str(plist)
    assert "KEY" not in str(plist)


def test_sigterm_pauses_and_terminates_fake_cli(tmp_path):
    root = tmp_path
    (root / ".venv/bin").mkdir(parents=True)
    (root / "runtime/paper-session").mkdir(parents=True)
    fake = root / ".venv/bin/crypto-agent"
    fake.write_text(
        f"#!{sys.executable}\nimport os,time,pathlib\n"
        f"pathlib.Path({str(root / 'child.pid')!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    fake.chmod(0o700)
    script = Path(module.__file__).resolve()
    driver = (
        "import importlib.util,signal\nfrom pathlib import Path\n"
        f"spec=importlib.util.spec_from_file_location('scheduler', {str(script)!r})\n"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
        f"s=m.Scheduler(Path({str(root)!r}))\n"
        "signal.signal(signal.SIGTERM,s.interrupted)\n"
        "m.save_json(s.inflight, {'test':True})\n"
        "s.invoke('auto-tick')\n"
    )
    process = subprocess.Popen([sys.executable, "-c", driver])
    try:
        deadline = time.monotonic() + 5
        while not (root / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (root / "child.pid").exists()
        pid = int((root / "child.pid").read_text())
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 128 + signal.SIGTERM
        assert (root / "runtime/paper-session/trading.auto-paused").exists()
        assert (root / "runtime/local-scheduler/inflight.json").exists()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_webhook_notification_uses_bearer_and_does_not_retry(tmp_path, monkeypatch):
    scheduler = module.Scheduler(tmp_path)
    from unittest.mock import MagicMock

    monkeypatch.setenv("CRYPTO_AGENT_NOTIFY_WEBHOOK", "http://127.0.0.1:5678/webhook/test")
    monkeypatch.setenv("CRYPTO_AGENT_NOTIFY_TOKEN", "private-test-token")
    response = MagicMock()
    response.__enter__.return_value.status = 200
    send = Mock(return_value=response)
    monkeypatch.setattr(module.urllib.request, "urlopen", send)
    module.Scheduler.notify(scheduler, "Paper fill")
    request = send.call_args.args[0]
    assert request.headers["Authorization"] == "Bearer private-test-token"
    assert json.loads(request.data)["message"] == "Paper fill"
    send.side_effect = RuntimeError("private-test-token")
    module.Scheduler.notify(scheduler, "Paper fault")
    assert send.call_count == 2
    assert "private-test-token" not in (scheduler.directory / "audit.jsonl").read_text()


def test_scheduler_heartbeat_records_cooldown_but_not_manual_check(scheduler):
    scheduler.invoke = Mock(side_effect=[(0, enabled()), (0, {"status": "cooldown"})])
    assert scheduler.run() == 0
    heartbeat = scheduler.directory / "heartbeat.json"
    assert datetime.fromisoformat(json.loads(heartbeat.read_text())["last_checked_at"])
    before = heartbeat.read_bytes()
    scheduler.invoke = Mock(return_value=(0, enabled()))
    assert scheduler.run(check_only=True) == 0
    assert heartbeat.read_bytes() == before
