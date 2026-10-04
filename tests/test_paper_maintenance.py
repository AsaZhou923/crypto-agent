"""Autonomous recovery never bypasses stops, orders, protected code or holdouts."""

import json
import runpy
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from crypto_agent.automation import Automation
from crypto_agent.config import load_settings
from crypto_agent.models import AssetRules
from crypto_agent.storage.database import Database

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/paper_maintenance.py"))
Maintenance = MODULE["Maintenance"]
MaintenanceError = MODULE["MaintenanceError"]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for name in MODULE["COPY_DIRS"]:
        (root / name).mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md", MODULE["BROKER_PATH"], *MODULE["DIRECT_REPAIRS"]):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(name, target)
    config = root / "runtime/paper-session"
    config.mkdir(parents=True)
    scheduler = root / "runtime/local-scheduler"
    scheduler.mkdir()
    for name in ("paper", "strategy", "risk"):
        shutil.copy2(f"config/deployment/paper-session/{name}.yaml", config / f"{name}.yaml")
    (root / ".env").write_text("# private config must not enter candidate\n")
    settings = load_settings(config, root=root)
    db = Database(settings.database_path, "paper")
    auto = Automation(settings, db)
    auto.enable(explicit=True)
    db.close()
    (scheduler / "policy.json").write_text(json.dumps({"config_digest": settings.digest}))
    maintenance = Maintenance(root)
    run_dir = root / "runtime/review-test"
    run_dir.mkdir()
    units = {
        name: {"ActiveState": "active", "UnitFileState": "enabled", "Result": "success"}
        for name in (MODULE["PAPER_TIMER"], MODULE["PAPER_SERVICE"], MODULE["DASHBOARD"])
    }
    units[MODULE["PAPER_SERVICE"]]["ActiveState"] = "inactive"
    monkeypatch.setattr(maintenance, "unit", lambda name: units[name].copy())

    def operation(*args):
        if args[0] == "stop":
            units[args[-1]]["ActiveState"] = "inactive"
        if args[0] in {"start", "restart"}:
            units[args[-1]]["ActiveState"] = "inactive" if args[-1] == MODULE["PAPER_SERVICE"] else "active"
        if args == ("start", "--no-block", MODULE["PAPER_SERVICE"]):
            with sqlite3.connect(maintenance.database) as db:
                db.execute(
                    "INSERT INTO auto_cycles VALUES ('verification','2026-10-04T03:00:00+00:00','2026-10-04T03:01:00+00:00','no_order',NULL,?)",
                    (
                        json.dumps(
                            {
                                "results": [
                                    {
                                        "preview": {
                                            "decision": {
                                                "rating": "Hold",
                                                "reason": "Fixture",
                                                "evaluation_eligible": True,
                                            }
                                        }
                                    }
                                ]
                            }
                        ),
                    ),
                )
        return ""

    maintenance.systemctl = Mock(side_effect=operation)
    return maintenance, run_dir, units


def test_enabled_timer_and_dashboard_recover_without_creating_orders(setup):
    maintenance, _, units = setup
    units[MODULE["PAPER_TIMER"]]["ActiveState"] = "inactive"
    units[MODULE["DASHBOARD"]]["ActiveState"] = "failed"
    result = maintenance.preflight()
    assert [a["action"] for a in result["actions"]] == ["restart_dashboard", "start_enabled_paper_timer"]
    maintenance.systemctl.assert_any_call("start", MODULE["PAPER_TIMER"])


@pytest.mark.parametrize("stop", ["manual_pause", "disabled_timer", "unknown_order", "inflight"])
def test_recovery_preserves_explicit_stop_and_unresolved_submission(setup, stop):
    maintenance, _, units = setup
    units[MODULE["PAPER_TIMER"]]["ActiveState"] = "inactive"
    if stop == "manual_pause":
        maintenance.pause.write_text("Paused by user\n")
    elif stop == "disabled_timer":
        units[MODULE["PAPER_TIMER"]]["UnitFileState"] = "disabled"
    elif stop == "inflight":
        (maintenance.scheduler / "inflight.json").write_text("{}")
    else:
        with sqlite3.connect(maintenance.database) as db:
            db.execute(
                "INSERT INTO orders(client_order_id,run_id,intent,status,attempted,updated_at) VALUES ('test','test','{}','unknown',1,'2026-10-04')"
            )
    assert maintenance.preflight()["actions"] == []
    maintenance.systemctl.assert_not_called()


def test_candidate_excludes_credentials_ledger_and_existing_tests_are_sealed(setup):
    maintenance, run_dir, _ = setup
    test = maintenance.root / "tests/test_original.py"
    test.write_text("def test_original(): assert True\n")
    candidate, manifest = maintenance.candidate(run_dir)
    assert not (candidate / ".env").exists() and not (candidate / "runtime").exists()
    (candidate / "tests/test_original.py").write_text("def test_original(): pass\n")
    with pytest.raises(MaintenanceError, match="protected"):
        maintenance.validate_candidate(candidate, manifest)


def test_broker_ast_seals_submission_and_accepts_only_market_reader_change(setup):
    maintenance, run_dir, _ = setup
    candidate, manifest = maintenance.candidate(run_dir)
    path = candidate / MODULE["BROKER_PATH"]
    original = path.read_text()
    path.write_text(
        original.replace('"Alpaca minute bars are missing or invalid"', '"Alpaca minute bars are invalid"')
    )
    assert maintenance.validate_candidate(candidate, manifest) == [MODULE["BROKER_PATH"]]
    path.write_text(original.replace("self.allow_submit = allow_submit", "self.allow_submit = True"))
    with pytest.raises(MaintenanceError, match="protected broker"):
        maintenance.validate_candidate(candidate, manifest)
    path.write_text(
        original.replace(
            '"""Read recent Alpaca US minute bars without substituting synthetic data."""',
            '"""Read recent Alpaca US minute bars without substituting synthetic data."""\n        self.allow_submit = True',
        )
    )
    with pytest.raises(MaintenanceError, match="protected broker"):
        maintenance.validate_candidate(candidate, manifest)


def test_environment_removes_all_application_keys_and_tokens(monkeypatch):
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("UNRELATED_API_KEY", "test-secret")
    monkeypatch.setenv("CRYPTO_AGENT_NOTIFY_TOKEN", "test-secret")
    env = MODULE["clean_environment"]()
    assert not any(
        name in env for name in ("ALPACA_SECRET_KEY", "UNRELATED_API_KEY", "CRYPTO_AGENT_NOTIFY_TOKEN")
    )


def repair_candidate(maintenance, run_dir):
    candidate, manifest = maintenance.candidate(run_dir)
    name = "src/crypto_agent/data/coinbase.py"
    path = candidate / name
    path.write_text(path.read_text() + "\n# regression-tested reader repair\n")
    return candidate, manifest, [name]


def test_deployment_binds_new_cohort_preserves_reduced_budget_and_backs_up(setup):
    maintenance, run_dir, _ = setup
    with sqlite3.connect(maintenance.database) as db:
        body = json.loads(db.execute("SELECT value FROM metadata WHERE key='automatic_policy'").fetchone()[0])
        body["current_multiplier"] = "0.5"
        db.execute("UPDATE metadata SET value=? WHERE key='automatic_policy'", (json.dumps(body),))
    old = load_settings(maintenance.config, root=maintenance.root)
    candidate, manifest, changed = repair_candidate(maintenance, run_dir)
    result = maintenance.deploy(candidate, manifest, changed, run_dir, old.digest)
    new = load_settings(maintenance.config, root=maintenance.root)
    assert result["status"] == "applied" and result["resumed"]
    assert new.digest != old.digest and new.risk == old.risk and new.paper == old.paper
    assert maintenance.state()["current_multiplier"] == "0.5"
    assert maintenance.state()["approval_digest"] == new.digest
    assert (run_dir / "production-backup/trading.sqlite").is_file()
    maintenance.systemctl.assert_any_call("start", "--no-block", MODULE["PAPER_SERVICE"])


def test_manual_pause_prevents_deployment_without_changing_source(setup):
    maintenance, run_dir, _ = setup
    candidate, manifest, changed = repair_candidate(maintenance, run_dir)
    original = (maintenance.root / changed[0]).read_bytes()
    maintenance.pause.write_text("Paused by user\n")
    result = maintenance.deploy(candidate, manifest, changed, run_dir)
    assert result["status"] == "deferred"
    assert (maintenance.root / changed[0]).read_bytes() == original
    assert maintenance.pause.read_text() == "Paused by user\n"
    maintenance.systemctl.assert_not_called()


def test_failed_verification_restores_code_and_config_without_overwriting_ledger(setup):
    maintenance, run_dir, units = setup
    candidate, manifest, changed = repair_candidate(maintenance, run_dir)
    source = (maintenance.root / changed[0]).read_bytes()
    strategy = (maintenance.config / "strategy.yaml").read_bytes()
    operation = maintenance.systemctl.side_effect

    def fail_tick(*args):
        output = operation(*args)
        if args == ("start", "--no-block", MODULE["PAPER_SERVICE"]):
            units[MODULE["PAPER_SERVICE"]]["Result"] = "exit-code"
            with sqlite3.connect(maintenance.database) as db:
                db.execute("INSERT INTO fills VALUES ('observed-during-validation','2026-10-04',NULL,'{}')")
        return output

    maintenance.systemctl.side_effect = fail_tick
    result = maintenance.deploy(candidate, manifest, changed, run_dir)
    assert result["status"] == "blocked" and result["code_rolled_back"]
    assert (maintenance.root / changed[0]).read_bytes() == source
    assert (maintenance.config / "strategy.yaml").read_bytes() == strategy
    assert maintenance.pause.exists() and not maintenance.state()["enabled"]
    with sqlite3.connect(maintenance.database) as db:
        assert db.execute("SELECT count(*) FROM fills").fetchone()[0] == 1


def test_optimization_defers_without_network_while_manually_paused(setup, monkeypatch):
    maintenance, run_dir, _ = setup
    maintenance.pause.write_text("Paused by user\n")
    broker = Mock(side_effect=AssertionError("Must not read broker or evaluate during manual pause"))
    monkeypatch.setattr("crypto_agent.runner.make_broker", broker)
    assert maintenance.optimize(run_dir)["status"] == "deferred"
    broker.assert_not_called()


def test_periodic_optimizer_uses_existing_sample_gate_without_creating_trades(setup, monkeypatch):
    maintenance, run_dir, _ = setup
    broker = Mock()
    broker.get_asset_rules.side_effect = lambda symbol: AssetRules(
        symbol, Decimal(".0001"), Decimal(".0001"), Decimal(".01")
    )
    factory = Mock(return_value=broker)
    monkeypatch.setattr("crypto_agent.runner.make_broker", factory)
    result = maintenance.optimize(run_dir)
    assert result["status"] == "evaluated"
    assert result["current_multiplier"] == result["previous_multiplier"] == "1"
    assert all(
        value["status"] == "insufficient_evidence" and not value["holdout_evaluated"]
        for value in result["results"].values()
    )
    assert factory.call_args.kwargs["allow_submit"] is False
    with sqlite3.connect(maintenance.database) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    broker.close.assert_called_once()


def test_worker_fault_is_not_hidden_by_no_order_and_successful_service_exit(setup):
    maintenance, _, _ = setup
    with sqlite3.connect(maintenance.database) as db:
        db.execute(
            "INSERT INTO auto_cycles VALUES ('test','2026-10-04','2026-10-04','no_order',NULL,?)",
            (
                json.dumps(
                    {
                        "results": [
                            {
                                "preview": {
                                    "decision": {"reason": "Intraday AI worker failed; no order is allowed"}
                                }
                            }
                        ]
                    }
                ),
            ),
        )
    with pytest.raises(MaintenanceError, match="Model protocol fault persists"):
        maintenance.verify_cycle(["src/crypto_agent/strategies/_intraday_ai_worker.py"])
