"""Bounded control of the one approved local Paper LaunchAgent; never executes a tick."""

import fcntl
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from crypto_agent.config import load_settings
from crypto_agent.models import AgentError

LABEL = "com.ze.crypto-agent.paper"
SYMBOLS = ["BTC/USD", "XRP/USD"]
INTERVAL = 300


class ControlError(Exception):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


class SchedulerControl:
    def __init__(self, monitor, *, runner=subprocess.run, home=None, platform=None):
        self.monitor = monitor
        self.root = monitor.root.resolve()
        self.config = self.root / "runtime/paper-session"
        self.directory = self.root / "runtime/local-scheduler"
        self.database = self.config / "trading.sqlite"
        self.pause = self.database.with_suffix(".auto-paused")
        self.plist = (home or Path.home()) / "Library/LaunchAgents" / f"{LABEL}.plist"
        self.target = f"gui/{os.getuid()}/{LABEL}"
        self.domain = f"gui/{os.getuid()}"
        self.runner = runner
        self.platform = platform or sys.platform
        self.lock = threading.Lock()

    def supported(self):
        settings = self.monitor.settings
        return bool(
            not self.monitor.demo_mode
            and self.platform == "darwin"
            and settings
            and settings.mode == "paper"
            and settings.database_path.resolve() == self.database.resolve()
        )

    def command(self, args, timeout=5):
        try:
            return self.runner(
                args, cwd=self.root, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ControlError("本机调度命令失败或超时；请检查本地调度日志。", 503) from None

    def launch_state(self, *, verify=False):
        result = self.command(["/bin/launchctl", "print", self.target])
        if result.returncode:
            if "could not find service" in (result.stderr or "").lower():
                return False, False
            raise ControlError("无法读取 launchd 状态；请检查本机登录会话。", 503)
        if verify:
            expected = {
                "program": str(self.root / ".venv/bin/python"),
                "working directory": str(self.root),
                "run interval": "300 seconds",
            }
            for key, value in expected.items():
                match = re.search(r"^\s*" + re.escape(key) + r" = (.+)$", result.stdout, re.M)
                if not match or match.group(1).strip() != value:
                    raise ControlError("已加载任务与固定 Paper 调度不匹配；请先停止并核对安装。")
            match = re.search(r"^\s*arguments = \{\n(.*?)^\s*\}", result.stdout, re.M | re.S)
            arguments = [line.strip() for line in match.group(1).splitlines()] if match else []
            if arguments != [
                str(self.root / ".venv/bin/python"),
                str(self.root / "scripts/paper_schedule.py"),
            ]:
                raise ControlError("已加载任务参数不匹配；禁止启用交易。")
        return True, bool(re.search(r"^\s*state = running\s*$", result.stdout, re.M))

    def ledger(self):
        # No CLI/Database construction here: polling never migrates or writes the ledger.
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='automatic_policy'").fetchone()
            mode = db.execute("SELECT value FROM metadata WHERE key='mode'").fetchone()
            unknown = db.execute(
                "SELECT 1 FROM orders WHERE attempted=1 AND status IN ('unknown','submitting') LIMIT 1"
            ).fetchone()
        if not mode or mode[0] != "paper":
            raise ControlError("本地账本不是 Paper 账本，禁止控制。")
        state = json.loads(row[0]) if row else {}
        if not isinstance(state, dict):
            raise ControlError("本地自动交易状态损坏，需检查账本。")
        return state, bool(unknown)

    def approved(self, state):
        settings = load_settings(self.config, "paper", root=self.root)
        policy = json.loads((self.directory / "policy.json").read_text())
        with self.plist.open("rb") as stream:
            installed = plistlib.load(stream)
        expected = {
            "Label": LABEL,
            "ProgramArguments": [
                str(self.root / ".venv/bin/python"),
                str(self.root / "scripts/paper_schedule.py"),
            ],
            "WorkingDirectory": str(self.root),
            "StartInterval": INTERVAL,
            "RunAtLoad": False,
            "KeepAlive": False,
            "ProcessType": "Background",
            "Umask": 0o077,
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
            "StandardOutPath": str(self.directory / "launchd.stdout.log"),
            "StandardErrorPath": str(self.directory / "launchd.stderr.log"),
        }
        if (
            settings.mode != "paper"
            or settings.database_path.resolve() != self.database.resolve()
            or settings.paper["symbols"] != SYMBOLS
            or settings.paper.get("automatic_interval_seconds") != INTERVAL
            or settings.paper.get("automatic_symbols_per_cycle") != 2
            or policy
            != {
                "config_digest": settings.digest,
                "mode": "paper",
                "symbols": SYMBOLS,
                "interval_seconds": INTERVAL,
            }
            or state.get("approval_digest") != settings.digest
            or installed != expected
        ):
            raise ControlError("配置、已批准策略或已安装调度任务不匹配；请先核对本地调度配置。")

    def status(self):
        value = dict(
            available=self.supported(),
            enabled=False,
            loaded=False,
            running=False,
            can_start=False,
            can_stop=False,
            state="unavailable",
            reason=None,
            interval_seconds=INTERVAL,
            symbols=SYMBOLS.copy(),
            as_of=datetime.now(UTC).isoformat(),
            error=None,
        )
        if not value["available"]:
            value["reason"] = "仅本机 macOS 的固定 Paper 会话支持启停；演示及其他配置不可控制。"
            return value
        try:
            value["loaded"], value["running"] = self.launch_state()
            value["can_stop"] = value["loaded"] or value["running"]
            state, unknown = self.ledger()
            value["enabled"] = state.get("enabled") is True and not self.pause.exists()
            value["can_stop"] = value["enabled"] or value["loaded"] or value["running"]
            value["state"] = (
                "running"
                if value["running"]
                else "waiting"
                if value["enabled"] and value["loaded"]
                else "paused"
                if self.pause.exists()
                else "stopped"
            )
            if (self.directory / "inflight.json").exists() and not value["running"]:
                raise ControlError("上次执行尚未核对完成（inflight）；请核对账本和平台订单后再恢复。")
            if unknown:
                raise ControlError("存在未知或提交中的订单；请先核对平台和账本，禁止重复提交。")
            self.approved(state)
            if value["loaded"]:
                self.launch_state(verify=True)
            value["can_start"] = not value["enabled"] and not value["running"]
            if value["enabled"] and not value["loaded"]:
                value["can_start"] = True
                value["state"] = "stopped"
                value["reason"] = "交易授权仍在，但 launchd 未加载；当前不会定时运行。"
            elif value["state"] == "paused":
                value["reason"] = "自动交易已暂停；已有平台订单保持原状。"
            elif value["state"] == "waiting":
                value["reason"] = "等待 launchd 下一轮定时触发。"
        except ControlError as exc:
            value.update(state="blocked", reason=str(exc), error=str(exc))
        except AgentError:
            value.update(
                state="blocked",
                reason="面板无法校验当前策略配置；若刚更新过策略代码，请重启面板服务。",
                error="策略配置校验失败；交易实际启用状态见开关，面板不会自动启停交易。",
            )
        except Exception:
            value.update(
                state="blocked",
                reason="本地调度配置或账本不可读；请检查本地文件。",
                error="本地调度状态读取失败。",
            )
        return value

    def write_pause(self):
        # Same persistent switch as Automation.pause(), even during a tick holding the DB lock.
        with self.pause.open("w") as stream:
            stream.write("Paused by dashboard\n")
            stream.flush()
            os.fsync(stream.fileno())

    @contextmanager
    def file_lock(self, name):
        with (self.directory / name).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ControlError("已有调度或控制操作进行中，请稍后核对状态。") from None
            yield

    def record(self, action, result):
        try:
            with (self.directory / "control-audit.jsonl").open("a") as stream:
                stream.write(
                    json.dumps({"at": datetime.now(UTC).isoformat(), "action": action, "result": result})
                    + "\n"
                )
        except OSError:
            pass  # Audit failure must never prevent an emergency pause.

    def apply(self, action):
        if action not in {"start", "stop"}:
            raise ControlError("不支持的控制动作。")
        if not self.supported():
            raise ControlError("当前实例不支持控制本机 Paper 调度。", 503)
        if not self.lock.acquire(blocking=False):
            raise ControlError("已有启停操作进行中，请稍后核对状态。")
        succeeded = False
        try:
            with self.file_lock("control.lock"):
                if action == "stop":
                    # Do not acquire scheduler.lock: an active tick owns it and must be stoppable.
                    self.write_pause()
                    loaded, _ = self.launch_state()
                    if loaded:
                        result = self.command(["/bin/launchctl", "bootout", self.target], timeout=10)
                        if result.returncode:
                            raise ControlError("交易已暂停，但 launchd 卸载失败；请检查本机调度状态。", 503)
                    stopped = self.status()
                    loaded, running = self.launch_state()
                    if loaded or running or stopped["enabled"] or stopped["error"]:
                        raise ControlError("暂停已写入，但调度停止尚未确认；请核对本机状态。", 503)
                    succeeded = True
                    return stopped
                with self.file_lock("scheduler.lock"):
                    state, unknown = self.ledger()
                    if (self.directory / "inflight.json").exists() or unknown:
                        raise ControlError("存在未核对执行或未知提交；请先核对既有订单，不能恢复。")
                    self.approved(state)
                    loaded, running = self.launch_state(verify=True)
                    if running:
                        raise ControlError("当前调度正在运行，无需重复启动。")
                    if loaded and state.get("enabled") is True and not self.pause.exists():
                        succeeded = True
                        return self.status()
                    try:
                        # Bootstrap while paused, with RunAtLoad=False; never trigger a tick here.
                        self.write_pause()
                        if not loaded:
                            result = self.command(
                                ["/bin/launchctl", "bootstrap", self.domain, str(self.plist)], timeout=10
                            )
                            if result.returncode:
                                raise ControlError("launchd 加载失败；交易保持暂停。", 503)
                        result = self.command(
                            [
                                str(self.root / ".venv/bin/crypto-agent"),
                                "--config",
                                str(self.config),
                                "--mode",
                                "paper",
                                "auto-enable",
                                "--execute-paper",
                            ],
                            timeout=20,
                        )
                        try:
                            enabled = json.loads(result.stdout).get("enabled") is True
                        except (ValueError, AttributeError):
                            enabled = False
                        if result.returncode or not enabled:
                            raise ControlError("交易授权启用失败；交易保持暂停，请检查本地日志。", 503)
                        result_state = self.status()
                        if result_state["error"] or not result_state["loaded"] or not result_state["enabled"]:
                            raise ControlError("启动结果未能确认；已恢复暂停，请核对本机状态。", 503)
                        succeeded = True
                        return result_state
                    except Exception:
                        self.write_pause()
                        raise
        except ControlError:
            raise
        except Exception:
            raise ControlError("本地启停失败；请核对暂停标记和调度状态。", 503) from None
        finally:
            self.record(action, "success" if succeeded else "failed")
            self.lock.release()
