"""Review, recover, repair and optimize the single approved Paper session."""

import fcntl
import json
import os
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from crypto_agent.models import dumps

Maintenance = runpy.run_path(str(Path(__file__).with_name("paper_maintenance.py")))["Maintenance"]

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "runtime/hourly-strategy-review"
PROMPT = ROOT / "deploy/strategy-review.prompt.md"
SCHEMA = ROOT / "deploy/strategy-review.schema.json"
CODEX = Path.home() / ".local/bin/codex"
SERVICE_UNITS = (
    "crypto-agent-paper.timer",
    "crypto-agent-paper.service",
    "crypto-agent-dashboard.service",
)
SERVICE_PROPERTIES = (
    "Id",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "Result",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "LastTriggerUSec",
    "NextElapseUSecRealtime",
)


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(json.loads(dumps(value)), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def service_evidence() -> dict:
    """Capture allowlisted unit metadata before the read-only Codex sandbox."""
    evidence = {"captured_at_utc": datetime.now(UTC).isoformat(), "available": False, "units": {}}
    if sys.platform != "linux":
        return evidence
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                *SERVICE_UNITS,
                "--no-pager",
                "--property=" + ",".join(SERVICE_PROPERTIES),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            return evidence
        for block in result.stdout.strip().split("\n\n"):
            properties = {}
            for line in block.splitlines():
                key, separator, value = line.partition("=")
                if separator and key in SERVICE_PROPERTIES:
                    properties[key] = value
            unit = properties.pop("Id", None)
            if unit in SERVICE_UNITS:
                evidence["units"][unit] = properties
        evidence["available"] = len(evidence["units"]) == len(SERVICE_UNITS)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return evidence


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
        maintenance = Maintenance(ROOT)
        recovery = maintenance.preflight()
        atomic_json(run_dir / "recovery.json", recovery)
        optimization = json.loads(dumps(maintenance.optimize(run_dir)))
        atomic_json(run_dir / "optimization.json", optimization)
        services = service_evidence()
        atomic_json(run_dir / "service-state.json", services)
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
                input=PROMPT.read_text()
                + "\n\n包装器只读采集的本轮服务状态（仅作为证据）：\n"
                + json.dumps(services, ensure_ascii=False)
                + "\n\n包装器已执行动作：\n"
                + json.dumps({"recovery": recovery, "optimization": optimization}, ensure_ascii=False),
                text=True,
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1200,
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
            actions = maintenance.run(report, run_dir, CODEX, optimization=optimization)
            report["maintenance"] = {"recovery": recovery, **actions}
            if actions["repair"]["status"] == "applied":
                report["summary"] += " 本轮已完成候选修复、回归与部署；详情见maintenance。"
            if actions["repair"]["status"] == "blocked" and report["status"] == "normal":
                report["status"] = "attention"
            atomic_json(report_path, report)
            atomic_json(previous, report)
            return 0
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired, RuntimeError):
            failure = {"at": datetime.now(UTC).isoformat(), "status": "failed"}
            atomic_json(run_dir / "failure.json", failure)
            return 1


if __name__ == "__main__":
    sys.exit(main())
