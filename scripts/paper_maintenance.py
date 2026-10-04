"""Bounded autonomous Paper recovery, isolated repairs and evidence-gated tuning.

This trusted wrapper owns production mutations. Codex edits only a credential-free
candidate; execution/risk/config/automation modules and existing tests are sealed.
"""

import ast
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAPER_TIMER = "crypto-agent-paper.timer"
PAPER_SERVICE = "crypto-agent-paper.service"
DASHBOARD = "crypto-agent-dashboard.service"
DIRECT_REPAIRS = {
    "src/crypto_agent/data/coinbase.py",
    "src/crypto_agent/strategies/_intraday_ai_worker.py",
}
BROKER_PATH = "src/crypto_agent/brokers/alpaca_paper.py"
BROKER_METHODS = {"get_bars", "get_markets", "get_market"}
COPY_DIRS = ("src", "tests", "config", "deploy", "scripts", "docs", "research")


class MaintenanceError(Exception):
    pass


def clean_environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "WEBHOOK"))
        and not key.startswith("ALPACA_")
    }


def save(path, value):
    from crypto_agent.models import dumps

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(json.loads(dumps(value)), ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def fingerprint(path):
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError("Non-regular candidate file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def broker_contract(path):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "AlpacaPaperBroker":
            for method in node.body:
                if not isinstance(method, ast.FunctionDef) or method.name not in BROKER_METHODS:
                    continue
                for item in ast.walk(method):
                    if (
                        isinstance(item, ast.Attribute)
                        and isinstance(item.value, ast.Name)
                        and item.value.id == "self"
                    ):
                        if item.attr not in {
                            "_request",
                            "symbols",
                            "_bar_provider",
                            "get_markets",
                        } or isinstance(item.ctx, ast.Store):
                            raise MaintenanceError("Market reader touched protected broker behavior")
                    if (
                        isinstance(item, ast.Call)
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr == "_request"
                    ):
                        if (
                            not item.args
                            or not isinstance(item.args[0], ast.Constant)
                            or item.args[0].value != "GET"
                        ):
                            raise MaintenanceError("Market reader attempted a broker write")
            node.body = [
                item
                for item in node.body
                if not (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in BROKER_METHODS
                )
            ]
    return ast.dump(tree, include_attributes=False)


@contextmanager
def exclusive(path):
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MaintenanceError("Another operation holds the maintenance lock") from None
        yield stream


class Maintenance:
    def __init__(self, root=ROOT):
        self.root = root.resolve()
        self.config = self.root / "runtime/paper-session"
        self.database = self.config / "trading.sqlite"
        self.pause = self.database.with_suffix(".auto-paused")
        self.scheduler = self.root / "runtime/local-scheduler"

    def state(self):
        if not self.database.is_file():
            raise MaintenanceError("Paper database unavailable")
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            body = db.execute("SELECT value FROM metadata WHERE key='automatic_policy'").fetchone()
            mode = db.execute("SELECT value FROM metadata WHERE key='mode'").fetchone()
            if not body or not mode or mode[0] != "paper":
                raise MaintenanceError("Approved Paper policy unavailable")
            state = json.loads(body[0])
            state["unknown_orders"] = db.execute(
                "SELECT count(*) FROM orders WHERE attempted=1 AND status IN ('unknown','submitting')"
            ).fetchone()[0]
            state["active_cycles"] = db.execute(
                "SELECT count(*) FROM auto_cycles WHERE status='started'"
            ).fetchone()[0]
        state["pause_reason"] = self.pause.read_text() if self.pause.exists() else None
        state["inflight"] = (self.scheduler / "inflight.json").exists()
        return state

    def systemctl(self, *args):
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise MaintenanceError("User service operation failed")
        return result.stdout

    def unit(self, name):
        output = self.systemctl(
            "show", name, "--property=ActiveState,SubState,UnitFileState,Result,ExecMainStatus"
        )
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def preflight(self):
        actions = []
        try:
            state = self.state()
            dashboard = self.unit(DASHBOARD)
            if dashboard.get("UnitFileState") == "enabled" and dashboard.get("ActiveState") not in {
                "active",
                "activating",
            }:
                self.systemctl("restart", DASHBOARD)
                actions.append({"action": "restart_dashboard", "status": "applied"})
            safe = state.get("enabled") and not any(
                state.get(key)
                for key in ("pause_reason", "unknown_orders", "inflight", "active_cycles", "halt_reason")
            )
            timer = self.unit(PAPER_TIMER)
            if safe and timer.get("UnitFileState") == "enabled" and timer.get("ActiveState") == "inactive":
                # Standard manual pause disables execution in the policy/kill switch.
                # Never enable a disabled timer or change any unit configuration.
                self.systemctl("start", PAPER_TIMER)
                actions.append({"action": "start_enabled_paper_timer", "status": "applied"})
            elif safe and timer.get("ActiveState") == "active":
                heartbeat = self.scheduler / "heartbeat.json"
                if heartbeat.exists():
                    checked = datetime.fromisoformat(json.loads(heartbeat.read_text())["last_checked_at"])
                    if (datetime.now(UTC) - checked).total_seconds() > 1260 and self.unit(PAPER_SERVICE).get(
                        "ActiveState"
                    ) == "inactive":
                        self.systemctl("start", "--no-block", PAPER_SERVICE)
                        actions.append({"action": "recover_stale_heartbeat", "status": "applied"})
            return {"status": "checked", "actions": actions}
        except (
            MaintenanceError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            sqlite3.Error,
            subprocess.TimeoutExpired,
        ):
            return {"status": "unavailable", "actions": actions}

    def optimize(self, run_dir):
        from crypto_agent.automation import Automation
        from crypto_agent.config import load_settings
        from crypto_agent.models import AgentError
        from crypto_agent.runner import make_broker
        from crypto_agent.storage.database import Database

        broker = db = None
        try:
            state = self.state()
            if (
                not state.get("enabled")
                or state.get("pause_reason")
                or state.get("unknown_orders")
                or state.get("inflight")
            ):
                return {"status": "deferred", "reason": "Paused or unresolved Paper session"}
            settings = load_settings(self.config, root=self.root)
            if state.get("approval_digest") != settings.digest:
                return {"status": "deferred", "reason": "Unapproved configuration"}
            broker = make_broker(settings, allow_submit=False)
            # Network reads precede locks, so an upstream delay cannot block ticks.
            assets = [broker.get_asset_rules(symbol) for symbol in settings.paper["symbols"]]
            with exclusive(self.database.with_suffix(".auto-cycle.lock")):
                db = Database(self.database, "paper")
                auto = Automation(settings, db)
                with db.lock():
                    auto._assert_enabled()
                    before = auto._state()
                    save(run_dir / "optimization-policy-before.json", before)
                    outcomes = {asset.symbol: auto._evaluate(asset) for asset in assets}
                    after = auto._state()
            return {
                "status": "evaluated",
                "previous_multiplier": before["current_multiplier"],
                "current_multiplier": after["current_multiplier"],
                "results": outcomes,
            }
        except (MaintenanceError, AgentError, OSError, ValueError, sqlite3.Error):
            return {"status": "deferred", "reason": "Data unavailable or trading cycle active"}
        finally:
            if broker is not None:
                broker.close()
            if db is not None:
                db.close()

    def candidate(self, run_dir):
        # Outside the production Git ancestry: Codex must not infer the live
        # repository as its writable workspace root.
        candidate = Path(tempfile.mkdtemp(prefix="crypto-agent-repair-"))
        save(run_dir / "candidate-location.json", {"path": str(candidate)})
        for name in COPY_DIRS:
            shutil.copytree(
                self.root / name, candidate / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        for name in ("pyproject.toml", "uv.lock", "README.md"):
            shutil.copy2(self.root / name, candidate / name)
        # No .env, notification credentials, git metadata, venv or trading ledger.
        manifest = {
            str(p.relative_to(candidate)): fingerprint(p) for p in candidate.rglob("*") if p.is_file()
        }
        save(run_dir / "candidate-manifest.json", manifest)
        return candidate, manifest

    def validate_candidate(self, candidate, manifest):
        changed = []
        files = {
            str(p.relative_to(candidate)): p for p in candidate.rglob("*") if p.is_file() or p.is_symlink()
        }
        for name, original in manifest.items():
            if name not in files:
                raise MaintenanceError("Candidate deleted a protected file")
            if fingerprint(files[name]) == original:
                continue
            if name == BROKER_PATH:
                if broker_contract(files[name]) != broker_contract(self.root / name):
                    raise MaintenanceError("Candidate changed protected broker behavior")
            elif name not in DIRECT_REPAIRS:
                raise MaintenanceError("Candidate changed a protected file")
            changed.append(name)
        for name, path in files.items():
            if (
                name in manifest
                or name.startswith((".pytest_cache/", ".ruff_cache/"))
                or "__pycache__" in path.parts
            ):
                continue
            if name == "repair-result.json":
                continue
            if not name.startswith("tests/test_") or path.suffix != ".py":
                raise MaintenanceError("Unexpected candidate file")
            fingerprint(path)
            changed.append(name)
        if len(changed) > 6 or sum(files[name].stat().st_size for name in changed) > 150_000:
            raise MaintenanceError("Candidate repair exceeds bounded scope")
        return changed

    def test_candidate(self, candidate, run_dir):
        env = clean_environment()
        env["PYTHONPATH"] = str(candidate / "src")
        python = str(self.root / ".venv/bin/python")
        for name, args in (
            ("lint", [python, "-m", "ruff", "check", "src", "tests"]),
            ("tests", [python, "-m", "pytest", "-q"]),
        ):
            result = subprocess.run(args, cwd=candidate, env=env, capture_output=True, timeout=180)
            # Keep summaries; third-party exception output never goes into the report.
            save(run_dir / f"candidate-{name}.json", {"exit_code": result.returncode})
            if result.returncode:
                raise MaintenanceError("Candidate regression checks failed")

    def prepare_repair(self, report, run_dir, codex):
        from crypto_agent.config import load_settings

        base_digest = load_settings(self.config, root=self.root).digest
        save(run_dir / "repair-base.json", {"config_digest": base_digest})
        candidate, manifest = self.candidate(run_dir)
        prompt = (self.root / "deploy/strategy-repair.prompt.md").read_text()
        prompt += "\n\n脱敏检查结果：\n" + json.dumps(report, ensure_ascii=False)
        prompt += f"\n生产根目录（只读，不得写入）：{self.root}\n"
        result = subprocess.run(
            [
                str(codex),
                "exec",
                "--sandbox",
                "workspace-write",
                "-c",
                'approval_policy="never"',
                "--skip-git-repo-check",
                "--ephemeral",
                "--output-schema",
                str(self.root / "deploy/strategy-repair.schema.json"),
                "--output-last-message",
                str(candidate / "repair-result.json"),
                "-",
            ],
            input=prompt,
            cwd=candidate,
            env=clean_environment(),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=600,
        )
        if result.returncode:
            raise MaintenanceError("Candidate agent failed")
        response = json.loads((candidate / "repair-result.json").read_text())
        changed = self.validate_candidate(candidate, manifest)
        if response.get("status") != "fixed" or not any(name.startswith("src/") for name in changed):
            return {"status": "no_change", "reason": "No validated repair within automatic scope"}
        self.test_candidate(candidate, run_dir)
        # Tests may produce files; verify the candidate again after executing them.
        self.validate_candidate(candidate, manifest)
        return self.deploy(candidate, manifest, changed, run_dir, base_digest)

    def verify_cycle(self, changed):
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT status,body FROM auto_cycles ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if not row or row[0] in {"started", "failed", "unknown", "halted", "submitting"}:
            raise MaintenanceError("No completed healthy verification cycle")
        body = json.loads(row[1] or "{}")
        decisions = [item.get("preview", {}).get("decision", {}) for item in body.get("results", [body])]
        if "src/crypto_agent/strategies/_intraday_ai_worker.py" in changed and any(
            any(
                fault in d.get("reason", "")
                for fault in ("worker failed", "timed out", "malformed", "model output")
            )
            for d in decisions
        ):
            raise MaintenanceError("Model protocol fault persists after repair")
        return {
            "cycle_status": row[0],
            "eligible_decisions": sum(d.get("evaluation_eligible") is True for d in decisions),
        }

    def deploy(self, candidate, manifest, changed, run_dir, base_digest=None):
        with exclusive(self.root / "runtime/deployment.lock"):
            timer_active = self.unit(PAPER_TIMER).get("ActiveState") == "active"
            try:
                return self._deploy_locked(candidate, manifest, changed, run_dir, base_digest)
            finally:
                current = self.state()
                if (
                    timer_active
                    and current.get("enabled")
                    and not any(current.get(k) for k in ("pause_reason", "unknown_orders", "halt_reason"))
                ):
                    self.systemctl("start", PAPER_TIMER)

    def _deploy_locked(self, candidate, manifest, changed, run_dir, base_digest):
        from crypto_agent.automation import Automation
        from crypto_agent.config import load_settings
        from crypto_agent.models import decimal
        from crypto_agent.storage.database import Database

        # API controls share this lock. Waiting ticks are stopped without killing
        # an active cycle; any unknown/inflight condition forbids deployment.
        with exclusive(self.scheduler / "control.lock") as control:
            before = self.state()
            current_settings = load_settings(self.config, root=self.root)
            if current_settings.digest != before.get("approval_digest") or (
                base_digest is not None and base_digest != current_settings.digest
            ):
                return {"status": "deferred", "reason": "Configuration changed or not approved"}
            if before.get("unknown_orders") or before.get("inflight"):
                return {"status": "deferred", "reason": "Unresolved submission or active tick"}
            recoverable = (
                before.get("halt_reason")
                == "Three consecutive failed cycles; automatic execution paused for diagnosis"
            )
            if (before.get("pause_reason") or not before.get("enabled")) and not recoverable:
                return {"status": "deferred", "reason": "Manual pause or protected stop preserved"}
            for name in changed:
                if name in manifest and fingerprint(self.root / name) != manifest[name]:
                    return {"status": "deferred", "reason": "Production changed during repair"}
            timer_active = self.unit(PAPER_TIMER).get("ActiveState") == "active"
            self.systemctl("stop", PAPER_TIMER)
            deadline = time.monotonic() + 120
            while self.unit(PAPER_SERVICE).get("ActiveState") in {"active", "activating"}:
                if time.monotonic() >= deadline:
                    if timer_active:
                        self.systemctl("start", PAPER_TIMER)
                    return {"status": "deferred", "reason": "Active cycle did not finish naturally"}
                time.sleep(2)
            before = self.state()
            if before.get("unknown_orders") or before.get("inflight") or before.get("active_cycles"):
                if timer_active:
                    self.systemctl("start", PAPER_TIMER)
                return {"status": "deferred", "reason": "Reconciliation required"}
            current_settings = load_settings(self.config, root=self.root)
            if current_settings.digest != before.get("approval_digest") or (
                base_digest is not None and base_digest != current_settings.digest
            ):
                return {"status": "deferred", "reason": "Configuration changed while waiting for idle"}
            # Manual pause and daily-loss stops are never auto-resumed.
            resume = recoverable or (
                before.get("enabled") and not before.get("pause_reason") and not before.get("halt_reason")
            )
            backup = run_dir / "production-backup"
            backup.mkdir(mode=0o700)
            settings = load_settings(self.config, root=self.root)
            marker = f"Autonomous maintenance {run_dir.name}\n"
            originals = {}
            paths = [*changed, "runtime/paper-session/strategy.yaml", "runtime/local-scheduler/policy.json"]
            for name in paths:
                target = self.root / name
                originals[name] = target.read_bytes() if target.exists() else None
                if target.exists():
                    dest = backup / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, dest)
            with (
                sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as src,
                sqlite3.connect(backup / "trading.sqlite") as dst,
            ):
                src.backup(dst)
                if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise MaintenanceError("Backup integrity check failed")
            db = Database(self.database, "paper")
            try:
                auto = Automation(settings, db)
                if resume:
                    auto.pause(marker.strip())
                save(
                    run_dir / "deployment.json",
                    {"status": "applying", "files": changed, "resume": bool(resume)},
                )
                for name in changed:
                    target = self.root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_suffix(target.suffix + ".maintenance-tmp")
                    shutil.copy2(candidate / name, temporary)
                    temporary.replace(target)
                # Bind changed signal/data semantics to a new observation cohort.
                strategy_path = self.config / "strategy.yaml"
                import yaml

                strategy = yaml.safe_load(strategy_path.read_text())
                strategy["maintenance_revision"] = hashlib.sha256(
                    "".join(
                        name + fingerprint(candidate / name)
                        for name in sorted(changed)
                        if name.startswith("src/")
                    ).encode()
                ).hexdigest()
                strategy_path.write_text(yaml.safe_dump(strategy, sort_keys=False))
                new_settings = load_settings(self.config, root=self.root)
                if resume and (not self.pause.exists() or self.pause.read_text() != marker):
                    raise MaintenanceError("Pause state changed during deployment")
                if resume:
                    auto = Automation(new_settings, db)
                    auto.enable(explicit=True)
                    updated = auto._state()
                    updated["current_multiplier"] = str(
                        min(decimal(updated["current_multiplier"]), decimal(before["current_multiplier"]))
                    )
                    auto._save(updated)
                    save(
                        self.scheduler / "policy.json",
                        {
                            "config_digest": new_settings.digest,
                            "mode": "paper",
                            "symbols": new_settings.paper["symbols"],
                            "interval_seconds": 300,
                        },
                    )
                self.systemctl("restart", DASHBOARD)
                # The atomic deployment is complete. Release API control now so
                # the user's stop button stays available during verification.
                fcntl.flock(control, fcntl.LOCK_UN)
                # A single ordinary bounded tick validates deployment; no forced orders.
                if resume and before.get("last_started_at"):
                    from crypto_agent.models import timestamp

                    next_allowed = timestamp(before["last_started_at"]).timestamp() + new_settings.paper.get(
                        "automatic_interval_seconds", 300
                    )
                    while time.time() < next_allowed and not self.pause.exists():
                        time.sleep(min(2, next_allowed - time.time()))
                if resume and not self.pause.exists():
                    self.systemctl("start", "--no-block", PAPER_SERVICE)
                    deadline = time.monotonic() + 180
                    while self.unit(PAPER_SERVICE).get("ActiveState") in {"active", "activating"}:
                        if time.monotonic() >= deadline:
                            raise MaintenanceError("Post-deployment tick exceeded verification bound")
                        time.sleep(2)
                    current = self.state()
                    if (
                        current.get("unknown_orders")
                        or current.get("pause_reason")
                        or self.unit(PAPER_SERVICE).get("Result") != "success"
                    ):
                        raise MaintenanceError("Post-deployment tick failed")
                    verification = self.verify_cycle(changed)
                    self.systemctl("start", PAPER_TIMER)
                else:
                    verification = {"status": "deferred", "reason": "User paused during maintenance"}
                outcome = {
                    "status": "applied",
                    "files": changed,
                    "config_digest": new_settings.digest,
                    "backup": str(backup.relative_to(self.root)),
                    "resumed": bool(resume and not self.pause.exists()),
                    "verification": verification,
                }
                save(run_dir / "deployment.json", outcome)
                return outcome
            except Exception:
                # Never overwrite the ledger, cancel orders, or kill a live tick.
                # If verification is still active, preserve files and pause instead.
                if not self.pause.exists() or self.pause.read_text() == marker:
                    auto.pause("Autonomous maintenance verification failed; reconciliation required")
                live = self.unit(PAPER_SERVICE).get("ActiveState") in {"active", "activating"}
                if not live:
                    for name, body in originals.items():
                        if body is not None:
                            (self.root / name).write_bytes(body)
                    restored = load_settings(self.config, root=self.root)
                    policy = auto._state()
                    policy.update(
                        approval_digest=restored.digest,
                        enabled=False,
                        halt_reason="Autonomous maintenance verification failed; reconciliation required",
                    )
                    policy["current_multiplier"] = str(
                        min(decimal(policy["current_multiplier"]), decimal(before["current_multiplier"]))
                    )
                    auto._save(policy)
                    self.systemctl("restart", DASHBOARD)
                outcome = {
                    "status": "blocked",
                    "reason": "Deployment failed; trading paused",
                    "code_rolled_back": not live,
                    "backup": str(backup.relative_to(self.root)),
                }
                save(run_dir / "deployment.json", outcome)
                return outcome
            finally:
                db.close()

    def run(self, report, run_dir, codex, optimization=None):
        result = {
            "optimization": self.optimize(run_dir) if optimization is None else optimization,
            "repair": {"status": "not_needed"},
        }
        # Reuse the project's audited Decimal/datetime serialization at every
        # boundary, including prompt construction and the final combined report.
        from crypto_agent.models import dumps

        result = json.loads(dumps(result))
        if report.get("repair_requested") is True:
            try:
                result["repair"] = self.prepare_repair(report, run_dir, codex)
            except (MaintenanceError, OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired):
                result["repair"] = {
                    "status": "blocked",
                    "reason": "Repair failed; check candidate and deployment audit",
                }
        save(run_dir / "maintenance.json", result)
        return result
