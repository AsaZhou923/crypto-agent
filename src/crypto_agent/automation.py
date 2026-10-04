"""One bounded scheduled cycle with durable cadence, a kill switch and audited optimization.

Scheduling belongs to an external trigger. This module never installs a daemon or
starts its own retry loop. Every invocation performs one bounded serial symbol round.
"""

import fcntl
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from crypto_agent.config import Settings
from crypto_agent.models import (
    TERMINAL_STATUSES,
    AgentError,
    AssetRules,
    BrokerRateLimited,
    BrokerReadUnavailable,
    OrderRejected,
    decimal,
    dumps,
    timestamp,
    utcnow,
)
from crypto_agent.runner import cancel_order, execute_preview, reconcile_account, run_once
from crypto_agent.storage.database import Database

INTERVAL_SECONDS = 600
MINIMUM_EVALUATION_SAMPLES = 146
ORDINARY_PRE_SUBMIT_REASONS = frozenset(
    {
        "Buy limit exceeds slippage bound or is below ask",
        "Sell limit exceeds slippage bound or is above bid",
        "Market data is stale",
        "Account data is stale",
        "Intraday minute bars are stale",
        "Decision is expired",
        "Price moved beyond preview tolerance; create a new preview",
        "Preview sizing changed with account/market; create a new preview",
        "Buy does not match the decision target",
        "Sell does not match the decision target",
    }
)
ALLOWED_MULTIPLIERS = (Decimal("1"), Decimal("0.75"), Decimal("0.5"))


class Automation:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.db = database
        self.pause_path = database.path.with_suffix(".auto-paused")
        self.cycle_lock_path = database.path.with_suffix(".auto-cycle.lock")
        database.connection.executescript("""
            CREATE TABLE IF NOT EXISTS auto_cycles (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                status TEXT NOT NULL, run_id TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS strategy_observations (
                id INTEGER PRIMARY KEY, run_id TEXT UNIQUE NOT NULL,
                body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS strategy_evaluations (
                id INTEGER PRIMARY KEY, evaluated_at TEXT NOT NULL,
                first_observation_id INTEGER NOT NULL, last_observation_id INTEGER NOT NULL,
                body TEXT NOT NULL);
        """)

    @property
    def interval_seconds(self) -> int:
        return self.settings.paper.get("automatic_interval_seconds", INTERVAL_SECONDS)

    @property
    def symbols_per_cycle(self) -> int:
        return self.settings.paper.get("automatic_symbols_per_cycle", 1)

    @property
    def evaluation_gap_seconds(self) -> int:
        rotations = (
            len(self.settings.paper["symbols"]) + self.symbols_per_cycle - 1
        ) // self.symbols_per_cycle
        return self.interval_seconds * (rotations + 1)

    def _state(self) -> dict:
        body = self.db.metadata("automatic_policy")
        return json.loads(body) if body else {"enabled": False}

    def _save(self, state: dict):
        with self.db.connection:
            self.db.connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('automatic_policy',?)", (dumps(state),)
            )

    def status(self) -> dict:
        state = self._state()
        row = self.db.connection.execute(
            "SELECT id,started_at,ended_at,status,run_id FROM auto_cycles ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        count = self.db.connection.execute("SELECT count(*) FROM strategy_observations").fetchone()[0]
        counts_by_symbol = {symbol: 0 for symbol in self.settings.paper["symbols"]}
        observations = []
        for observation in self.db.connection.execute(
            "SELECT id,body FROM strategy_observations ORDER BY id"
        ):
            body = json.loads(observation["body"])
            observations.append((observation["id"], body))
            symbol = body.get("symbol", "BTC/USD")
            if body.get("config_digest") == self.settings.digest and symbol in counts_by_symbol:
                counts_by_symbol[symbol] += 1
        diagnostics = self._evaluation_diagnostics(observations)
        last_evaluation, last_by_symbol = self._last_evaluations(diagnostics)
        return {
            "enabled": state.get("enabled", False) and not self.pause_path.exists(),
            "mode": self.settings.mode,
            "interval_seconds": self.interval_seconds,
            "symbols_per_cycle": self.symbols_per_cycle,
            "evaluation_gap_seconds": self.evaluation_gap_seconds,
            "state": state,
            "kill_switch_present": self.pause_path.exists(),
            "last_cycle": dict(row) if row else None,
            "observation_count": count,
            "observation_count_by_symbol": counts_by_symbol,
            "minimum_evaluation_samples": MINIMUM_EVALUATION_SAMPLES,
            "last_evaluation": last_evaluation,
            "last_evaluation_by_symbol": last_by_symbol,
            "evaluation_diagnostics_by_symbol": diagnostics,
        }

    def enable(self, *, explicit: bool = False) -> dict:
        if not explicit:
            raise AgentError("Automatic execution requires the explicit flag for its environment")
        if self.settings.paper["trigger"] != "scheduled":
            raise AgentError("Automatic execution requires trigger: scheduled in the selected configuration")
        if self.settings.mode == "paper" and self.settings.paper["trading_enabled"] is not True:
            raise AgentError("Paper execution must be explicitly enabled in local configuration")
        if self.settings.strategy["timeout_seconds"] > 480:
            raise AgentError(
                "Scheduled analysis timeout must be at most 480 seconds to leave time for reconciliation"
            )
        with self.db.lock():
            state = self._state()
            same_policy = state.get("approval_digest") == self.settings.digest
            latest_observation_id = self.db.connection.execute(
                "SELECT COALESCE(max(id),0) FROM strategy_observations"
            ).fetchone()[0]
            initial_cursor = state.get("evaluation_cursor", 0) if same_policy else latest_observation_id
            state = {
                "enabled": True,
                "approved_at": utcnow().isoformat(),
                "approval_digest": self.settings.digest,
                "interval_seconds": self.interval_seconds,
                "symbols_per_cycle": self.symbols_per_cycle,
                "base_targets": self.settings.strategy["rating_target_pct"],
                "current_multiplier": state.get("current_multiplier", "1") if same_policy else "1",
                "last_started_at": state.get("last_started_at"),
                "read_retry_not_before": state.get("read_retry_not_before"),
                "evaluation_cursor": initial_cursor,
                "evaluation_cursors": state.get("evaluation_cursors", {})
                if same_policy
                else {symbol: latest_observation_id for symbol in self.settings.paper["symbols"]},
                "next_symbol_index": state.get("next_symbol_index", 0) if same_policy else 0,
                "failure_count": 0,
                "halt_reason": None,
            }
            self._save(state)
            self.pause_path.unlink(missing_ok=True)
        return self.status()

    def pause(self, reason: str = "Paused by user") -> dict:
        # Write immediately without waiting for a model call holding the DB lock.
        # The submission wrapper checks this again immediately before POST.
        self.pause_path.write_text(reason + "\n")
        return {
            "enabled": False,
            "status": "paused",
            "message": reason,
            "note": "Prevents new submissions; any already accepted order still needs reconciliation",
        }

    def _assert_enabled(self):
        state = self._state()
        if not state.get("enabled") or self.pause_path.exists():
            raise AgentError("Automatic execution is paused")
        if state["approval_digest"] != self.settings.digest:
            self.pause("Configuration changed; explicitly enable the new policy before execution")
            raise AgentError("Automatic configuration differs from the enabled policy")

    def _halt(self, reason: str):
        self.pause(reason)
        state = self._state()
        state.update(enabled=False, halt_reason=reason)
        self._save(state)

    def _finish(self, cycle_id: str, result: dict, failure: bool = False) -> dict:
        state = self._state()
        state["failure_count"] = state.get("failure_count", 0) + 1 if failure else 0
        self._save(state)
        if failure and state["failure_count"] >= 3:
            self._halt("Three consecutive failed cycles; automatic execution paused for diagnosis")
            result["automatic_paused"] = True
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE auto_cycles SET ended_at=?,status=?,run_id=?,body=? WHERE id=?",
                (utcnow().isoformat(), result["status"], result.get("run_id"), dumps(result), cycle_id),
            )
        return result

    def _effective_settings(self) -> Settings:
        state = self._state()
        factor = decimal(state["current_multiplier"])
        if factor not in ALLOWED_MULTIPLIERS:
            raise AgentError("Invalid automatic policy multiplier")
        targets = {key: decimal(value) * factor for key, value in state["base_targets"].items()}
        strategy = {**self.settings.strategy, "rating_target_pct": targets}
        if strategy.get("intraday_entry_policy") == "capped_probe":
            for key in ("intraday_probe_max_position_usd", "intraday_probe_cost_budget_usd"):
                strategy[key] = decimal(strategy[key]) * factor
        return replace(self.settings, strategy=strategy)

    def tick(self, broker, *, explicit: bool = False, strategy=None) -> dict:
        if not explicit:
            raise AgentError("A scheduled tick requires the explicit execution flag")
        with self.cycle_lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {
                    "status": "busy",
                    "message": "Previous cycle is still running; no new analysis or order",
                }
            try:
                return self._tick(broker, strategy=strategy)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _tick(self, broker, strategy=None) -> dict:
        self._assert_enabled()
        state = self._state()
        now = utcnow()
        if state.get("last_started_at"):
            due = timestamp(state["last_started_at"]) + timedelta(seconds=self.interval_seconds)
            if now < due:
                return {
                    "status": "cooldown",
                    "next_eligible_at": due,
                    "message": "No catch-up or extra cycle",
                }
        if state.get("read_retry_not_before") and now < timestamp(state["read_retry_not_before"]):
            return {
                "status": "cooldown",
                "next_eligible_at": state["read_retry_not_before"],
                "message": "Waiting after a temporary read failure; no replay or submission retry",
            }
        # Claim before network access. A restart cannot replay this time slot.
        with self.db.lock():
            symbols = self.settings.paper["symbols"]
            symbol_index = int(state.get("next_symbol_index", 0)) % len(symbols)
            selected_symbols = [
                symbols[(symbol_index + offset) % len(symbols)] for offset in range(self.symbols_per_cycle)
            ]
            state["last_started_at"] = now.isoformat()
            state["next_symbol_index"] = (symbol_index + self.symbols_per_cycle) % len(symbols)
            self._save(state)
            cycle_id = uuid4().hex
            with self.db.connection:
                self.db.connection.execute(
                    "UPDATE auto_cycles SET status='interrupted',ended_at=? WHERE status='started'",
                    (now.isoformat(),),
                )
                self.db.connection.execute(
                    "INSERT INTO auto_cycles(id,started_at,status) VALUES (?,?,'started')",
                    (cycle_id, now.isoformat()),
                )
        results = []
        for selected_symbol in selected_symbols:
            result = self._run_symbol(broker, selected_symbol, strategy=strategy)
            if self.symbols_per_cycle > 1:
                result.setdefault("symbol", selected_symbol)
            results.append(result)
            # Only an unsubmitted, symbol-local stale quote can skip ahead.
            # Pending submissions, account risk, pauses and faults end the round.
            if (
                result["status"] not in {"filled", "no_order"}
                and not result.get("symbol_data_unavailable", False)
            ) or self.pause_path.exists():
                break
        result = results[-1]
        failure = any(
            item["status"] in {"blocked", "failed", "unknown", "rejected"}
            and not item.get("ordinary_pre_submit_skip", False)
            for item in results
        )
        if self.symbols_per_cycle > 1:
            status = result["status"]
            if any(item["status"] == "blocked" for item in results) and status in {"filled", "no_order"}:
                status = "blocked"
            if all(item["status"] in {"filled", "no_order"} for item in results):
                status = "filled" if any(item["status"] == "filled" for item in results) else "no_order"
            result = {
                "status": status,
                "results": results,
                "selected_symbols": selected_symbols,
                "skipped_symbols": selected_symbols[len(results) :],
                "report": self.db.report(),
            }
            if self.pause_path.exists():
                result["automatic_paused"] = True
        return self._finish(cycle_id, result, failure=failure)

    def _run_symbol(self, broker, selected_symbol: str, strategy=None) -> dict:
        guarded = _GuardedBroker(broker, self._assert_enabled)
        executing_preview = False
        try:
            self._assert_enabled()
            reconciled = reconcile_account(guarded, self.db)
            tracked = self.db.orders(attempted_only=True)
            for row in tracked:
                if row["status"] in {"unknown", "submitting"}:
                    self._halt("An order has an uncertain submission outcome; reconcile before resuming")
                    return {"status": "halted", "reason": "unknown_order", "automatic_paused": True}
            try:
                daily = self.db.daily_baseline(utcnow())
            except AgentError:
                daily = None  # A validated fresh run establishes the first UTC-day baseline.
            if daily is not None and (
                daily - reconciled["portfolio"].equity_usd >= self.settings.risk["max_daily_loss_usd"]
            ):
                # Persist the stop before network cancellation. Cancellation alone
                # remains permitted here to remove already accepted exposure.
                self._halt("Observed daily loss limit reached; explicit resume is required")
                cancellations = []
                for row in tracked:
                    if row["status"] not in TERMINAL_STATUSES:
                        try:
                            cancel_order(broker, self.db, row["client_order_id"], explicit=True)
                            outcome = self.db.order_for_run(row["run_id"])["status"]
                        except Exception:
                            outcome = "reconciliation_required"
                        cancellations.append({"client_order_id": row["client_order_id"], "status": outcome})
                return {
                    "status": "halted",
                    "reason": "daily_loss_limit",
                    "automatic_paused": True,
                    "cancellations": cancellations,
                    "report": self.db.report(),
                }
            for row in tracked:
                if row["status"] not in TERMINAL_STATUSES:
                    decision = self.db.get_run(row["run_id"])["decision"]
                    if decision is None or timestamp(decision.expires_at) <= utcnow():
                        self._assert_enabled()
                        guarded.cancel_order(row["client_order_id"])
            reconciled = reconcile_account(guarded, self.db)
            if reconciled["portfolio"].open_orders or any(
                row["status"] not in TERMINAL_STATUSES for row in self.db.orders(attempted_only=True)
            ):
                return {"status": "pending", "message": "Outstanding orders: no new analysis or order"}
            self._assert_enabled()
            settings = self._effective_settings()
            preview = run_once(
                settings,
                guarded,
                self.db,
                strategy=strategy,
                symbol=selected_symbol,
                cycle_started_at=self._state()["last_started_at"],
            )
            result = {
                "status": preview["status"],
                "run_id": preview["run_id"],
                "symbol": selected_symbol,
                "preview": preview,
            }
            if preview["status"] in {"preview", "no_order"}:
                self._record_observation(preview, utcnow())
            if preview["status"] == "preview":
                self._assert_enabled()
                executing_preview = True
                executed = execute_preview(settings, guarded, self.db, preview["run_id"], explicit=True)
                executing_preview = False
                result["execution"] = executed
                row = self.db.order_for_run(preview["run_id"])
                result["status"] = row["status"] if row["attempted"] else executed.get("status", "blocked")
                if result["status"] in {"unknown", "submitting"}:
                    self._halt("An order has an uncertain submission outcome; reconcile before resuming")
                    result["automatic_paused"] = True
            if result["status"] == "blocked":
                risk = result.get("execution", {}).get("risk") or preview.get("risk")
                if risk and any("loss limit" in reason for reason in risk.reasons):
                    self._halt("Observed daily loss limit reached; explicit resume is required")
                    result["automatic_paused"] = True
            result["optimization"] = self._evaluate(guarded.get_asset_rules(selected_symbol))
            result["report"] = self.db.report()
            has_execution = "execution" in result
            if result["status"] == "blocked" and self._ordinary_pre_submit_skip(
                preview["run_id"],
                result.get("execution", {}).get("risk") or preview.get("risk"),
                allow_no_order=not has_execution,
            ):
                result["ordinary_pre_submit_skip"] = True
                if (
                    not has_execution
                    and self.db.order_for_run(preview["run_id"]) is None
                    and set(getattr(preview.get("risk"), "reasons", ())) == {"Market data is stale"}
                ):
                    result["symbol_data_unavailable"] = True
            return result
        except (BrokerRateLimited, BrokerReadUnavailable) as exc:
            # A GET can be deferred, but it cannot resolve a potentially accepted POST.
            if any(row["status"] in {"unknown", "submitting"} for row in self.db.orders(attempted_only=True)):
                self._halt("An order has an uncertain submission outcome; reconcile before resuming")
                return {"status": "halted", "reason": "unknown_order", "automatic_paused": True}
            state = self._state()
            delay = max(self.interval_seconds, exc.retry_after_seconds)
            state["read_retry_not_before"] = (utcnow() + timedelta(seconds=delay)).isoformat()
            self._save(state)
            return {
                "status": "rate_limited" if isinstance(exc, BrokerRateLimited) else "read_unavailable",
                "symbol": selected_symbol,
                "reason": str(exc),
                "retry_after_seconds": delay,
                "next_eligible_at": state["read_retry_not_before"],
                "message": "Read deferred; next eligible round reconciles orders and uses fresh data",
            }
        except AgentError as exc:
            if any(row["status"] in {"unknown", "submitting"} for row in self.db.orders(attempted_only=True)):
                self._halt("An order has an uncertain submission outcome; reconcile before resuming")
                return {"status": "halted", "reason": "unknown_order", "automatic_paused": True}
            row = self.db.order_for_run(preview["run_id"]) if "preview" in locals() else None
            if (
                executing_preview
                and row
                and row["attempted"] == 0
                and str(exc) in ORDINARY_PRE_SUBMIT_REASONS
            ):
                return {
                    "status": "blocked",
                    "run_id": preview["run_id"],
                    "reason": str(exc),
                    "ordinary_pre_submit_skip": True,
                }
            return {"status": "failed", "reason": str(exc)}
        except Exception:
            if any(row["status"] in {"unknown", "submitting"} for row in self.db.orders(attempted_only=True)):
                self._halt("An order has an uncertain submission outcome; reconcile before resuming")
                return {"status": "halted", "reason": "unknown_order", "automatic_paused": True}
            # No exception text from third-party SDKs or model clients is persisted.
            return {"status": "failed", "reason": "Unexpected cycle failure; reconciliation required"}

    def _ordinary_pre_submit_skip(self, run_id: str, risk, *, allow_no_order: bool = False) -> bool:
        reasons = getattr(risk, "reasons", ())
        if not reasons or set(reasons) - ORDINARY_PRE_SUBMIT_REASONS:
            return False
        row = self.db.order_for_run(run_id)
        return row["attempted"] == 0 if row else allow_no_order

    def _record_observation(self, preview: dict, available_at):
        decision = self.db.get_run(preview["run_id"])["decision"]
        if not decision.evaluation_eligible:
            return
        if decision.rating not in {"Buy", "Overweight", "Hold", "Underweight", "Sell", "REVIEW"}:
            return
        snapshot = self.db.latest_snapshot(decision.symbol)
        market = snapshot["market"]
        # A REVIEW can return before the runner refreshes its quote. Never label
        # a stale initial quote as a valid observation at decision completion.
        age = (timestamp(available_at) - timestamp(market["observed_at"])).total_seconds()
        if not 0 <= age <= float(self.settings.risk["max_data_age_seconds"]):
            return
        observation = {
            "observed_at": market["observed_at"],
            "available_at": available_at,
            "rating": decision.rating,
            "symbol": decision.symbol,
            "bid": market["bid"],
            "ask": market["ask"],
            "equity": snapshot["portfolio"]["equity_usd"],
            "config_digest": self.settings.digest,
            "strategy_version": decision.strategy_version,
            "model": decision.model,
            "mode": self.settings.mode,
            "actionable": decision.actionable or decision.rating == "Hold",
            "cycle_started_at": self._state()["last_started_at"],
        }
        with self.db.connection:
            self.db.connection.execute(
                "INSERT OR IGNORE INTO strategy_observations(run_id,body) VALUES (?,?)",
                (preview["run_id"], dumps(observation)),
            )

    def _evaluate(self, asset: AssetRules) -> dict:
        state = self._state()
        cursors = dict(state.get("evaluation_cursors", {}))
        cursor = int(
            cursors.get(asset.symbol, state.get("evaluation_cursor", 0) if asset.symbol == "BTC/USD" else 0)
        )
        rows = self.db.connection.execute(
            "SELECT id,body FROM strategy_observations WHERE id>? ORDER BY id",
            (cursor,),
        ).fetchall()
        rows = [row for row in rows if json.loads(row["body"]).get("symbol", "BTC/USD") == asset.symbol]
        if not rows:
            return {
                "status": "insufficient_evidence",
                "new_samples": len(rows),
                "required_samples": MINIMUM_EVALUATION_SAMPLES,
                "holdout_evaluated": False,
            }
        from crypto_agent.evaluation import Observation, evaluate_candidates

        observations = []
        bodies = []
        try:
            for row in rows:
                item = json.loads(row["body"])
                bodies.append(item)
                observations.append(Observation(**item))
            freshness = self._current_evaluation_freshness(
                bodies, enforce_stale=bool(self._state().get("last_started_at"))
            )
            if freshness is not None:
                return freshness | {
                    "new_samples": len(rows),
                    "required_samples": MINIMUM_EVALUATION_SAMPLES,
                    "holdout_evaluated": False,
                }
            if (
                observations[-1].config_digest != self.settings.digest
                or observations[-1].mode != self.settings.mode
            ):
                evaluated = {
                    "status": "invalid_evidence",
                    "holdout_evaluated": False,
                    "reason": "Latest evidence does not match the approved configuration and mode",
                }
            else:
                evaluated = evaluate_candidates(
                    observations,
                    deepcopy(state["base_targets"]),
                    deepcopy(self.settings.risk),
                    current_multiplier=decimal(state["current_multiplier"]),
                    asset=asset,
                    probe_config=deepcopy(self.settings.strategy)
                    if self.settings.strategy.get("intraday_entry_policy") == "capped_probe"
                    else None,
                    min_interval_seconds=self.interval_seconds,
                    max_gap_seconds=self.evaluation_gap_seconds,
                )
        except (TypeError, ValueError, AttributeError):
            evaluated = {
                "status": "invalid_evidence",
                "holdout_evaluated": False,
                "reason": "Malformed observation; no policy change or holdout consumption",
            }
        selected_start = int(evaluated.get("excluded_prefix_count", 0))
        if selected_start < 0 or selected_start >= len(rows):
            raise AgentError("Evaluation selected an invalid observation window")
        selected_first_id = rows[selected_start]["id"]
        selected_count = int(evaluated.get("observation_count", len(rows) - selected_start))
        if selected_count < 0 or selected_count > len(rows) - selected_start:
            raise AgentError("Evaluation selected an invalid observation count")
        evaluated.update(
            new_samples=selected_count,
            total_new_samples=len(rows),
            required_samples=MINIMUM_EVALUATION_SAMPLES,
            selected_first_observation_id=selected_first_id,
            selected_last_observation_id=rows[-1]["id"],
        )
        candidate = decimal(evaluated.get("recommended_multiplier", state["current_multiplier"]))
        # Evaluation cannot expand exposure, regardless of its output.
        if candidate not in ALLOWED_MULTIPLIERS or candidate > decimal(state["current_multiplier"]):
            raise AgentError("Evaluation proposed a forbidden exposure increase")
        holdout_evaluated = evaluated.get("holdout_evaluated") is True
        if holdout_evaluated and selected_count < MINIMUM_EVALUATION_SAMPLES:
            raise AgentError("Evaluation cannot examine a holdout below the sample gate")
        if evaluated.get("status") == "promote" and not holdout_evaluated:
            raise AgentError("Evaluation cannot promote without an examined holdout")
        if evaluated.get("status") == "promote" and candidate < decimal(state["current_multiplier"]):
            state["current_multiplier"] = str(candidate)
        # Consume only a genuinely examined holdout, including a rejected one.
        # Incomplete/invalid evidence remains available for future diagnostics.
        if holdout_evaluated:
            state["evaluation_cursor"] = rows[-1]["id"]
            cursors[asset.symbol] = rows[-1]["id"]
            state["evaluation_cursors"] = cursors
        with self.db.connection:
            self.db.connection.execute(
                "INSERT INTO strategy_evaluations(evaluated_at,first_observation_id,last_observation_id,body) VALUES (?,?,?,?)",
                (utcnow().isoformat(), selected_first_id, rows[-1]["id"], dumps(evaluated)),
            )
            self.db.connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('automatic_policy',?)", (dumps(state),)
            )
        return evaluated

    def _last_evaluations(self, diagnostics: dict) -> tuple[dict | None, dict]:
        overall = None
        by_symbol = {}
        wanted = set(self.settings.paper["symbols"])
        for row in self.db.connection.execute(
            "SELECT id,evaluated_at,first_observation_id,last_observation_id,body "
            "FROM strategy_evaluations ORDER BY id DESC"
        ):
            body = json.loads(row["body"])
            cohort = body.get("cohort") or {}
            evaluation = self._decorate_evaluation(row, body, diagnostics)
            if overall is None:
                overall = evaluation
            symbol = cohort.get("symbol")
            if symbol in wanted and symbol not in by_symbol:
                by_symbol[symbol] = evaluation
                if len(by_symbol) == len(wanted) and overall is not None:
                    break
        return overall, by_symbol

    def _decorate_evaluation(self, row, body: dict, diagnostics: dict) -> dict:
        return {
            **body,
            "audit": {
                "id": row["id"],
                "evaluated_at": row["evaluated_at"],
                "first_observation_id": row["first_observation_id"],
                "last_observation_id": row["last_observation_id"],
            },
            "current_status": self._evaluation_body_status(body, diagnostics),
        }

    def _evaluation_body_status(self, body: dict, diagnostics: dict) -> dict:
        cohort = body.get("cohort") or {}
        symbol = cohort.get("symbol")
        if symbol not in self.settings.paper["symbols"]:
            return {"status": "historical", "reason": "Evaluation has no current configured symbol"}
        if cohort.get("config_digest") != self.settings.digest or cohort.get("mode") != self.settings.mode:
            return {"status": "historical", "reason": "Evaluation used a different configuration or mode"}
        diagnostic = diagnostics[symbol]
        if diagnostic["status"] != "current":
            return diagnostic
        last_id = body.get("selected_last_observation_id")
        if last_id is not None and last_id != diagnostic.get("latest_observation_id"):
            return {
                **diagnostic,
                "status": "historical",
                "reason": "Evaluation does not include the latest current observation",
            }
        return diagnostic

    def _evaluation_diagnostics(self, observations: list[tuple[int, dict]]) -> dict:
        state = self._state()
        cursors = dict(state.get("evaluation_cursors", {}))
        result = {}
        for symbol in self.settings.paper["symbols"]:
            cursor = int(cursors.get(symbol, state.get("evaluation_cursor", 0) if symbol == "BTC/USD" else 0))
            raw_count = 0
            current_config_count = 0
            latest = None
            latest_id = None
            for row_id, body in observations:
                if body.get("symbol", "BTC/USD") != symbol:
                    continue
                if row_id > cursor:
                    raw_count += 1
                    if (
                        body.get("config_digest") == self.settings.digest
                        and body.get("mode") == self.settings.mode
                    ):
                        current_config_count += 1
                if (
                    body.get("config_digest") == self.settings.digest
                    and body.get("mode") == self.settings.mode
                ):
                    latest = body
                    latest_id = row_id
            diagnostic = {
                "status": "insufficient_evidence",
                "symbol": symbol,
                "raw_new_observations": raw_count,
                "current_config_observations": current_config_count,
                "required_samples": MINIMUM_EVALUATION_SAMPLES,
                "cursor": cursor,
                "max_gap_seconds": self.evaluation_gap_seconds,
                "latest_observation_id": latest_id,
                "latest_observation_available_at": latest.get("available_at") if latest else None,
            }
            if latest is None:
                diagnostic["reason"] = "No current-configuration observations after the evaluation cursor"
            elif freshness := self._current_evaluation_freshness([latest], enforce_stale=True):
                diagnostic.update(freshness)
            else:
                diagnostic["status"] = "current"
                diagnostic["reason"] = "Latest current-configuration observation is fresh"
            result[symbol] = diagnostic
        return result

    def _current_evaluation_freshness(self, observations: list[dict], *, enforce_stale: bool) -> dict | None:
        state = self._state()
        now = utcnow()
        try:
            latest_available_at = timestamp(observations[-1]["available_at"])
        except (KeyError, TypeError, ValueError, AgentError):
            return {
                "status": "invalid_evidence",
                "reason": "Malformed observation freshness; no policy change or holdout consumption",
                "latest_observation_available_at": None,
                "current_cycle_started_at": state.get("last_started_at"),
            }
        age = (now - latest_available_at).total_seconds()
        if age < 0:
            return {
                "status": "invalid_evidence",
                "reason": "Latest optimization evidence is from the future",
                "latest_observation_available_at": latest_available_at.isoformat(),
                "current_cycle_started_at": state.get("last_started_at"),
                "age_seconds": age,
                "max_gap_seconds": self.evaluation_gap_seconds,
            }
        if enforce_stale and age > self.evaluation_gap_seconds:
            return {
                "status": "insufficient_evidence",
                "reason": "Latest optimization evidence is stale; waiting for a fresh eligible observation",
                "latest_observation_available_at": latest_available_at.isoformat(),
                "current_cycle_started_at": state.get("last_started_at"),
                "age_seconds": age,
                "max_gap_seconds": self.evaluation_gap_seconds,
            }
        return None


class _GuardedBroker:
    def __init__(self, broker, guard):
        self._broker = broker
        self._guard = guard

    def __getattr__(self, name):
        return getattr(self._broker, name)

    def submit_order(self, order):
        try:
            self._guard()
        except AgentError:
            raise OrderRejected("Automatic execution paused before submission; no request sent") from None
        return self._broker.submit_order(order)

    def cancel_order(self, client_order_id):
        self._guard()
        return self._broker.cancel_order(client_order_id)
