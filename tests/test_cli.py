"""Fresh subprocess CLI end-to-end checks with synthetic local data only."""

import json
import shutil
import subprocess
import sys

import yaml

from crypto_agent.cli import _failed_outcome
from crypto_agent.models import RiskResult


def invoke(db, *command):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "crypto_agent.cli",
            "--config",
            "config/demo",
            "--mode",
            "offline",
            "--db",
            str(db),
            *command,
        ],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_offline_preview_execute_new_process_reconcile_report(tmp_path):
    db = tmp_path / "cli.sqlite"
    doctor = invoke(db, "doctor", "--connect")
    assert doctor["connection_checked"] and doctor["mode"] == "offline"
    preview = invoke(db, "preview")
    assert preview["status"] == "preview" and not preview["submitted"]
    invoke(db, "execute", preview["run_id"], "--execute-offline")
    invoke(db, "execute", preview["run_id"], "--execute-offline")
    assert invoke(db, "reconcile")["portfolio"]["positions"]
    report = invoke(db, "report")
    assert report["fill_count"] == 1 and report["ledger_matches_position"]
    assert invoke(db, "history")[0]["status"] == "filled"
    assert invoke(db, "preview")["status"] == "no_order"


def test_demo_needs_no_api_keys(tmp_path, monkeypatch):
    # Offline paths never create an HTTP client, even if local .env exists.
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    result = invoke(tmp_path / "demo.sqlite", "demo")
    assert result["report"]["fill_count"] == 1
    assert "SYNTHETIC TEST DATA" in result["notice"]


def test_nested_execution_failures_return_nonzero_predicate():
    assert _failed_outcome({"execution": {"status": "unknown"}})
    assert _failed_outcome({"execution": {"status": "rejected"}})
    assert _failed_outcome({"data_checks": RiskResult(False, ("stale",))})
    assert not _failed_outcome({"execution": {"status": "filled"}})


def test_automatic_cli_enable_tick_restart_cooldown_and_pause(tmp_path):
    config = tmp_path / "config"
    shutil.copytree("config/demo", config)
    paper_path = config / "paper.yaml"
    paper = yaml.safe_load(paper_path.read_text())
    paper.update(trigger="scheduled", database_path=str(tmp_path / "automatic.sqlite"))
    paper_path.write_text(yaml.safe_dump(paper))

    def call(*command, expected_code=0):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "crypto_agent.cli",
                "--config",
                str(config),
                "--mode",
                "offline",
                *command,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == expected_code, result.stderr or result.stdout
        return json.loads(result.stdout or result.stderr)

    assert call("auto-enable", "--execute-offline")["enabled"]
    assert call("auto-tick", "--execute-offline")["status"] == "filled"
    assert call("auto-tick", "--execute-offline")["status"] == "cooldown"
    assert call("report")["fill_count"] == 1
    assert call("auto-status")["enabled"]
    assert call("auto-pause")["status"] == "paused"
    assert not call("auto-status")["enabled"]
    assert "paused" in call("auto-tick", "--execute-offline", expected_code=2)["error"]
