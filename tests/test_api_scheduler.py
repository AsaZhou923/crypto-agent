"""Control tests use temporary Paper files and fake commands; never touch real launchd."""

import fcntl
import json
import plistlib
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from crypto_agent.api.app import create_app
from crypto_agent.api.scheduler import LABEL, ControlError, SchedulerControl
from crypto_agent.api.service import Monitor


@pytest.fixture
def control(tmp_path, monkeypatch):
    config = tmp_path / "runtime/paper-session"
    directory = tmp_path / "runtime/local-scheduler"
    config.mkdir(parents=True)
    directory.mkdir()
    database = config / "trading.sqlite"
    settings = SimpleNamespace(
        mode="paper",
        database_path=database,
        digest="approved",
        paper={
            "symbols": ["BTC/USD", "XRP/USD"],
            "automatic_interval_seconds": 300,
            "automatic_symbols_per_cycle": 2,
        },
    )
    monkeypatch.setattr("crypto_agent.api.scheduler.load_settings", lambda *a, **kw: settings)
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE orders (status TEXT, attempted INTEGER);"
        )
        db.execute("INSERT INTO metadata VALUES ('mode','paper')")
        db.execute(
            "INSERT INTO metadata VALUES ('automatic_policy',?)",
            (json.dumps({"enabled": True, "approval_digest": "approved"}),),
        )
    monitor = SimpleNamespace(root=tmp_path, settings=settings, demo_mode=False, close=lambda: None)
    instance = SchedulerControl(monitor, home=tmp_path, platform="darwin")
    instance.pause.write_text("Paused by user\n")
    instance.plist.parent.mkdir(parents=True)
    instance.plist.write_bytes(
        plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": [
                    str(tmp_path / ".venv/bin/python"),
                    str(tmp_path / "scripts/paper_schedule.py"),
                ],
                "WorkingDirectory": str(tmp_path),
                "StartInterval": 300,
                "RunAtLoad": False,
                "KeepAlive": False,
                "ProcessType": "Background",
                "Umask": 0o077,
                "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
                "StandardOutPath": str(directory / "launchd.stdout.log"),
                "StandardErrorPath": str(directory / "launchd.stderr.log"),
            }
        )
    )
    (directory / "policy.json").write_text(
        json.dumps(
            {
                "config_digest": "approved",
                "mode": "paper",
                "symbols": ["BTC/USD", "XRP/USD"],
                "interval_seconds": 300,
            }
        )
    )
    instance.calls = []
    instance.fake_loaded = True
    instance.fake_running = False
    instance.fail_on = None

    def run(args, **kwargs):
        assert kwargs["timeout"] <= 20 and kwargs.get("shell") is not True
        instance.calls.append(args)
        action = args[1] if args[0] == "/bin/launchctl" else "auto-enable"
        if instance.fail_on == action:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        if action == "print":
            if not instance.fake_loaded:
                return SimpleNamespace(returncode=113, stdout="", stderr="Could not find service")
            return SimpleNamespace(
                returncode=0,
                stdout=("state = running\n" if instance.fake_running else "state = not running\n")
                + f"program = {tmp_path}/.venv/bin/python\nworking directory = {tmp_path}\nrun interval = 300 seconds\narguments = {{\n{tmp_path}/.venv/bin/python\n{tmp_path}/scripts/paper_schedule.py\n}}\n",
                stderr="",
            )
        if action in {"bootstrap", "bootout"}:
            assert instance.pause.exists(), "pause must be durable before loading/stopping"
            instance.fake_loaded = action == "bootstrap"
            instance.fake_running = False
        elif action == "auto-enable":
            assert instance.fake_loaded
            assert args == [
                str(tmp_path / ".venv/bin/crypto-agent"),
                "--config",
                str(config),
                "--mode",
                "paper",
                "auto-enable",
                "--execute-paper",
            ]
            instance.pause.unlink()
        else:
            pytest.fail(f"Unexpected command: {action}")
        return SimpleNamespace(returncode=0, stdout='{"enabled":true}', stderr="")

    instance.runner = run
    return instance


def test_get_preserves_paused_files_and_ledger(control):
    before = {p: p.read_bytes() for p in control.root.rglob("*") if p.is_file()}
    status = control.status()
    assert status["state"] == "paused" and not status["enabled"]
    assert status["loaded"] and status["can_start"] and status["can_stop"]
    assert before == {p: p.read_bytes() for p in control.root.rglob("*") if p.is_file()}
    assert all(args[1] == "print" for args in control.calls)


def test_start_loads_then_enables_no_tick_and_is_idempotent(control):
    control.fake_loaded = False
    status = control.apply("start")
    assert status["state"] == "waiting" and status["enabled"]
    mutations = [args for args in control.calls if args[1] != "print"]
    assert mutations[0][1] == "bootstrap" and "auto-enable" in mutations[1]
    assert not any("auto-tick" in args or "kickstart" in args for args in control.calls)
    control.apply("start")
    assert sum("auto-enable" in args for args in control.calls) == 1


def test_stop_pauses_before_bootout_even_during_tick_lock(control):
    control.pause.unlink()
    control.fake_running = True
    with (control.directory / "scheduler.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = control.apply("stop")
    assert status["state"] == "paused" and not status["enabled"] and not status["loaded"]
    assert control.pause.read_text() == "Paused by dashboard\n"
    assert any(args[1] == "bootout" for args in control.calls)


@pytest.mark.parametrize("blocker", ["inflight", "unknown", "submitting", "digest", "plist", "locked"])
def test_start_refuses_unresolved_or_unapproved_state(control, blocker):
    lock = None
    if blocker == "inflight":
        (control.directory / "inflight.json").write_text('{"evidence":"preserve"}')
    elif blocker in {"unknown", "submitting"}:
        with sqlite3.connect(control.database) as db:
            db.execute("INSERT INTO orders VALUES (?,1)", (blocker,))
    elif blocker == "digest":
        control.monitor.settings.digest = "changed"
    elif blocker == "plist":
        value = plistlib.loads(control.plist.read_bytes())
        value["RunAtLoad"] = True
        control.plist.write_bytes(plistlib.dumps(value))
    else:
        lock = (control.directory / "scheduler.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ControlError):
            control.apply("start")
    finally:
        if lock:
            lock.close()
    assert control.pause.read_text() == "Paused by user\n"
    assert not any("auto-enable" in args or "bootstrap" in args for args in control.calls)
    if blocker == "inflight":
        assert (control.directory / "inflight.json").read_text() == '{"evidence":"preserve"}'


@pytest.mark.parametrize("failure", ["bootstrap", "auto-enable", "bootout"])
def test_command_failure_keeps_paused_without_retry(control, failure):
    control.fake_loaded = failure != "bootstrap"
    control.fail_on = failure
    with pytest.raises(ControlError):
        control.apply("stop" if failure == "bootout" else "start")
    assert control.pause.exists()
    assert sum((failure in args) for args in control.calls) == 1


def test_api_requires_exact_origin_header_and_whitelist(control):
    with TestClient(
        create_app(monitor=control.monitor, scheduler=control), base_url="http://127.0.0.1:8766"
    ) as client:
        valid = {"Origin": "http://127.0.0.1:8766", "X-Crypto-Agent-Control": "1"}
        for headers in (
            {},
            {"Origin": valid["Origin"]},
            {**valid, "Origin": "http://localhost:8766"},
            {**valid, "Origin": "http://127.0.0.1:9999"},
            {**valid, "Origin": "https://evil.example"},
        ):
            assert client.post("/api/scheduler", headers=headers, json={"action": "start"}).status_code == 403
        assert not control.calls
        assert client.post("/api/scheduler", headers=valid, json={"action": "tick"}).status_code == 422
        assert (
            client.post(
                "/api/scheduler", headers=valid, json={"action": "start", "command": "auto-tick"}
            ).status_code
            == 422
        )
        assert client.post("/api/orders", headers=valid, json={}).status_code == 405
        assert client.options("/api/scheduler", headers=valid).status_code == 405
        assert (
            client.post("/api/scheduler", headers=valid, json={"action": "stop"}).json()["enabled"] is False
        )
        assert client.get("/api/health").json()["read_only"] is False


def test_demo_cannot_control_or_create_files(tmp_path):
    monitor = Monitor(demo_mode=True, root=tmp_path)
    with TestClient(create_app(monitor=monitor), base_url="http://127.0.0.1") as client:
        assert client.get("/api/scheduler").json()["available"] is False
        assert (
            client.post(
                "/api/scheduler",
                headers={"Origin": "http://127.0.0.1", "X-Crypto-Agent-Control": "1"},
                json={"action": "start"},
            ).status_code
            == 503
        )
        assert client.get("/api/health").json()["read_only"] is True
    assert not list(tmp_path.iterdir())


def test_loaded_job_must_match_runtime_not_only_plist(control):
    run = control.runner

    def other_job(args, **kwargs):
        result = run(args, **kwargs)
        if args[1] == "print":
            result.stdout = result.stdout.replace("scripts/paper_schedule.py", "scripts/other.py")
        return result

    control.runner = other_job
    assert control.status()["can_start"] is False
    with pytest.raises(ControlError):
        control.apply("start")
    assert control.pause.read_text() == "Paused by user\n"
    assert not any("auto-enable" in args for args in control.calls)


def test_bootout_success_without_unloading_is_not_reported_as_stopped(control):
    run = control.runner

    def still_loaded(args, **kwargs):
        result = run(args, **kwargs)
        if args[1] == "bootout":
            control.fake_loaded = True
        return result

    control.runner = still_loaded
    with pytest.raises(ControlError, match="停止尚未确认"):
        control.apply("stop")
    assert control.pause.exists()
    event = json.loads((control.directory / "control-audit.jsonl").read_text().splitlines()[-1])
    assert event["action"] == "stop" and event["result"] == "failed"


def test_start_cli_success_without_real_enable_is_repaused(control):
    run = control.runner

    def no_enable(args, **kwargs):
        result = run(args, **kwargs)
        if "auto-enable" in args:
            control.pause.write_text("other controller paused\n")
        return result

    control.runner = no_enable
    with pytest.raises(ControlError, match="启动结果未能确认"):
        control.apply("start")
    assert control.pause.exists()
