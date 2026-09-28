"""One scheduler invocation, fixed to this project's authorized Paper session.

Never enables trading, retries a tick, changes policy, or invokes Codex.
"""

import argparse
import fcntl
import json
import logging
import os
import plistlib
import signal
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.ze.crypto-agent.paper"
INTERVAL = 300


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Scheduler:
    def __init__(self, root=ROOT):
        self.root = root
        self.config = root / "runtime/paper-session"
        self.directory = root / "runtime/local-scheduler"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pause = self.config / "trading.auto-paused"
        self.inflight = self.directory / "inflight.json"
        self.policy = self.directory / "policy.json"
        self.database = self.config / "trading.sqlite"
        self.active_process = None
        self.logger = logging.getLogger(f"paper-scheduler.{root}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = RotatingFileHandler(self.directory / "audit.jsonl", maxBytes=5_000_000, backupCount=5)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)

    def record(self, kind, **values):
        self.logger.info(json.dumps({"at": datetime.now(UTC).isoformat(), "kind": kind, **values}))

    def notify(self, message):
        webhook = os.environ.get("CRYPTO_AGENT_NOTIFY_WEBHOOK")
        if webhook:
            # No retries and no credential-bearing exception text in the audit.
            payload = json.dumps(
                {
                    "source": "crypto-agent",
                    "mode": "paper",
                    "message": message,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            ).encode()
            headers = {"Content-Type": "application/json"}
            token = os.environ.get("CRYPTO_AGENT_NOTIFY_TOKEN")
            if token:
                headers["Authorization"] = "Bearer " + token
            try:
                request = urllib.request.Request(webhook, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=10) as response:
                    if not 200 <= response.status < 300:
                        self.record("notification_unavailable")
            except Exception:
                self.record("notification_unavailable")
            return
        if sys.platform != "darwin":
            self.record("notification_unavailable")
            return
        # argv, not interpolated AppleScript; failure cannot cause a trading retry.
        script = (
            'on run argv\n display notification (item 1 of argv) with title "Crypto Agent · Paper"\nend run'
        )
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script, message],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode:
                self.record("notification_unavailable")
        except (OSError, subprocess.TimeoutExpired):
            self.record("notification_unavailable")

    def write_pause(self, reason):
        # Same persistent switch used by auto-pause, even if CLI/config is broken.
        with self.pause.open("w") as stream:
            stream.write(reason + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def interrupted(self, signum, _frame):
        # launchctl bootout/logout must not leave a detached CLI free to submit.
        # Keep inflight evidence: already accepted orders require reconciliation.
        try:
            self.write_pause("Local scheduler interrupted; reconcile before resuming")
        finally:
            process = self.active_process
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        raise SystemExit(128 + signum)

    def stop(self, reason):
        self.write_pause(reason)
        self.record("fault_paused", reason=reason)
        self.notify("模拟交易已暂停：" + reason)

    def invoke(self, command):
        args = [
            str(self.root / ".venv/bin/crypto-agent"),
            "--config",
            str(self.config),
            "--mode",
            "paper",
            command,
        ]
        if command == "auto-tick":
            args.append("--execute-paper")
        with subprocess.Popen(
            args,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            self.active_process = process
            try:
                stdout, _ = process.communicate(timeout=900 if command == "auto-tick" else 30)
            except subprocess.TimeoutExpired:
                # Pause BEFORE terminating: an accepted order still needs reconciliation.
                self.stop("CLI timeout; reconcile existing orders before resuming")
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise RuntimeError("CLI timeout") from None
            finally:
                self.active_process = None
        try:
            value = json.loads(stdout)
        except (ValueError, TypeError):
            raise RuntimeError("CLI did not return valid JSON; reconciliation required") from None
        if not isinstance(value, dict) or "error" in value:
            raise RuntimeError("CLI failed; inspect local ledger before resuming")
        return process.returncode, value

    def settings(self):
        from crypto_agent.config import load_settings

        settings = load_settings(self.config, "paper", root=self.root)
        if (
            settings.database_path.resolve() != self.database.resolve()
            or settings.paper["symbols"] != ["BTC/USD", "XRP/USD"]
            or settings.paper.get("automatic_interval_seconds") != INTERVAL
            or settings.paper.get("automatic_symbols_per_cycle") != 2
        ):
            raise RuntimeError("Local scheduler requires the approved two-symbol Paper session")
        return settings

    def orders(self):
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            result = {}
            for client_id, status, body in db.execute(
                "SELECT client_order_id,status,broker_json FROM orders WHERE attempted=1"
            ):
                order = json.loads(body) if body else {}
                result[client_id] = {
                    "status": status,
                    "symbol": order.get("symbol"),
                    "side": order.get("side"),
                    "filled_quantity": order.get("filled_quantity", "0"),
                }
            return result

    def report_changes(self, result):
        path = self.directory / "seen-orders.json"
        before = json.loads(path.read_text()) if path.exists() else {}
        after = self.orders()
        messages = []
        for key, order in after.items():
            previous = before.get(key, {})
            quantity = Decimal(order["filled_quantity"])
            if quantity > Decimal(previous.get("filled_quantity", "0")):
                messages.append(
                    f"{order['symbol']} {order['side']} 成交增加 "
                    f"{quantity - Decimal(previous.get('filled_quantity', '0'))}；"
                    f"状态 {order['status']}"
                )
            elif order["status"] == "rejected" and previous.get("status") != "rejected":
                messages.append(f"订单拒绝：{key}")
        for item in result.get("results", [result]):
            evaluation = item.get("optimization", {})
            if evaluation.get("status") == "promote":
                messages.append(
                    f"{item.get('symbol')} 参数倍数调整为 {evaluation.get('recommended_multiplier')}"
                )
        # Persist before notifying so an unavailable Notification Center never causes duplicates.
        save_json(path, after)
        for message in messages:
            self.record("event", message=message)
            self.notify("Paper " + message)

    def run(self, check_only=False):
        with (self.directory / "scheduler.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            try:
                code, status = self.invoke("auto-status")
                if code or type(status.get("enabled")) is not bool:
                    raise RuntimeError("Invalid auto-status response")
                self.record("status", enabled=status["enabled"], check_only=check_only)
                if not status["enabled"]:
                    return 0
                if self.inflight.exists():
                    raise RuntimeError("Previous local tick interrupted; reconcile before resuming")
                settings = self.settings()
                policy = json.loads(self.policy.read_text())
                if (
                    settings.digest != policy["config_digest"]
                    or settings.digest != status["state"]["approval_digest"]
                ):
                    raise RuntimeError("Configuration changed; review scheduler policy before resuming")
                if check_only:
                    return 0
                # Liveness is independent of order attempts: Retry-After can legitimately
                # keep last_started_at unchanged while each scheduled check succeeds.
                save_json(
                    self.directory / "heartbeat.json", {"last_checked_at": datetime.now(UTC).isoformat()}
                )
                last = status["state"].get("last_started_at")
                if last and (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() < INTERVAL:
                    return 0
                save_json(self.inflight, {"started_at": datetime.now(UTC).isoformat()})
                code, result = self.invoke("auto-tick")  # Exactly once. Never retry.
                self.record("tick", exit_code=code, result=result)
                items = result.get("results", [result])
                known = {
                    "filled",
                    "no_order",
                    "blocked",
                    "pending",
                    "partially_filled",
                    "partial",
                    "new",
                    "accepted",
                    "pending_new",
                    "accepted_for_bidding",
                    "pending_cancel",
                    "pending_replace",
                    "done_for_day",
                    "stopped",
                    "suspended",
                    "calculated",
                    "canceled",
                    "expired",
                    "replaced",
                    "cooldown",
                    "rate_limited",
                    "read_unavailable",
                    "busy",
                    "paused",
                    "rejected",
                    "failed",
                    "unknown",
                    "submitting",
                    "halted",
                }
                if (
                    code not in {0, 2}
                    or result.get("status") not in known
                    or not isinstance(items, list)
                    or not items
                    or any(item.get("status") not in known for item in items)
                ):
                    raise RuntimeError("Unrecognized tick outcome; reconciliation required")
                fault = result.get("automatic_paused") or any(
                    item.get("automatic_paused")
                    or item["status"] in {"failed", "unknown", "submitting", "halted"}
                    for item in items
                )
                if fault:
                    self.stop("CLI reported a fault or automatic stop; inspect audit before resuming")
                self.report_changes(result)
                self.inflight.unlink()
                return 1 if fault else 0
            except Exception:
                # Exception strings/SDK output may contain credentials. Do not log them.
                self.stop(
                    "Local scheduler failed or was interrupted; inspect audit and reconcile before resuming"
                )
                return 1


def launch_agent(root=ROOT):
    return {
        "Label": LABEL,
        "ProgramArguments": [str(root / ".venv/bin/python"), str(root / "scripts/paper_schedule.py")],
        "WorkingDirectory": str(root),
        "StartInterval": INTERVAL,
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Background",
        "Umask": 0o077,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(root / "runtime/local-scheduler/launchd.stdout.log"),
        "StandardErrorPath": str(root / "runtime/local-scheduler/launchd.stderr.log"),
    }


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Status only; never runs a tick")
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Write policy and platform scheduler units while trading is paused",
    )
    args = parser.parse_args()
    scheduler = Scheduler()
    signal.signal(signal.SIGTERM, scheduler.interrupted)
    signal.signal(signal.SIGINT, scheduler.interrupted)
    if args.prepare:
        code, status = scheduler.invoke("auto-status")
        if code or status.get("enabled") is not False:
            parser.error("Pause trading before preparing the scheduler")
        if scheduler.inflight.exists():
            parser.error("Reconcile interrupted tick before changing the scheduler")
        settings = scheduler.settings()
        if settings.digest != status["state"]["approval_digest"]:
            parser.error("Configuration must already have explicit approval")
        save_json(
            scheduler.policy,
            {
                "config_digest": settings.digest,
                "mode": "paper",
                "symbols": ["BTC/USD", "XRP/USD"],
                "interval_seconds": INTERVAL,
            },
        )
        save_json(scheduler.directory / "seen-orders.json", scheduler.orders())
        if sys.platform == "linux":
            from crypto_agent.api.scheduler import systemd_units

            for name, content in systemd_units(ROOT).items():
                target = scheduler.directory / name
                target.write_text(content)
                print(target)
        else:
            target = scheduler.directory / f"{LABEL}.plist"
            target.write_bytes(plistlib.dumps(launch_agent()))
            print(target)
        return 0
    return scheduler.run(check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
