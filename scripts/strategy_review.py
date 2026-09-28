"""Run a bounded, read-only Codex review of the single Paper session."""

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "runtime/hourly-strategy-review"
PROMPT = ROOT / "deploy/strategy-review.prompt.md"
SCHEMA = ROOT / "deploy/strategy-review.schema.json"
CODEX = Path.home() / ".local/bin/codex"


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def notify(message: str) -> None:
    from paper_schedule import Scheduler

    Scheduler(ROOT).notify(message)


def main() -> int:
    os.umask(0o077)
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    with (DIRECTORY / "server-review.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = DIRECTORY / stamp
        run_dir.mkdir(mode=0o700)
        report_path = run_dir / "report.json"
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("CRYPTO_AGENT_NOTIFY_", "ALPACA_", "OPENAI_API_KEY", "CODEX_API_KEY")):
                env.pop(key)
        try:
            result = subprocess.run(
                [
                    str(CODEX),
                    "exec",
                    "--sandbox",
                    "read-only",
                    "--output-schema",
                    str(SCHEMA),
                    "--output-last-message",
                    str(report_path),
                    "-",
                ],
                input=PROMPT.read_text(),
                text=True,
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1500,
                check=False,
            )
            if result.returncode:
                raise RuntimeError("Codex review exited unsuccessfully")
            report = json.loads(report_path.read_text())
            if (
                report.get("status") not in {"normal", "attention", "blocked"}
                or not isinstance(report.get("summary"), str)
                or not isinstance(report.get("findings"), list)
            ):
                raise ValueError("Invalid Codex review output")
            previous = DIRECTORY / "server-review-latest.json"
            prior = json.loads(previous.read_text()) if previous.exists() else None
            key = hashlib.sha256(
                json.dumps(
                    {
                        "status": report["status"],
                        "findings": [
                            (item["severity"], item["finding"], item["next_step"])
                            for item in report["findings"]
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            old_key = prior.get("notification_digest") if prior else None
            report["notification_digest"] = key
            atomic_json(previous, report)
            if report["status"] != "normal" and key != old_key:
                notify("Paper 两小时策略检查：" + report["summary"][:500])
            return 0
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired, RuntimeError):
            failure = {"at": datetime.now(UTC).isoformat(), "status": "failed"}
            atomic_json(run_dir / "failure.json", failure)
            notify("Paper 两小时策略检查未完成；请查看服务器 Codex 检查服务日志。")
            return 1


if __name__ == "__main__":
    sys.exit(main())
