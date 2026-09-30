"""Scheduled safety gates against local, persistent synthetic broker state only."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

import crypto_agent.automation as automation_module
from crypto_agent.automation import MINIMUM_EVALUATION_SAMPLES, Automation
from crypto_agent.brokers.alpaca_paper import AlpacaPaperBroker
from crypto_agent.brokers.offline import OfflineBroker
from crypto_agent.config import load_settings
from crypto_agent.models import (
    AgentError,
    BrokerError,
    BrokerRateLimited,
    BrokerReadUnavailable,
    SubmissionUnknown,
    dumps,
    timestamp,
    utcnow,
)
from crypto_agent.storage.database import Database
from crypto_agent.strategies.baseline import BaselineStrategy


@pytest.fixture
def setup(tmp_path):
    settings = load_settings(Path("config/demo"), mode="offline")
    settings = replace(
        settings,
        paper={
            **settings.paper,
            "database_path": str(tmp_path / "ledger.sqlite"),
            "trigger": "scheduled",
            "trading_enabled": True,
        },
    )
    db = Database(settings.database_path, "offline")
    broker = OfflineBroker(tmp_path / "broker.sqlite")
    automation = Automation(settings, db)
    yield settings, db, broker, automation
    broker.close()
    db.close()


def rating_strategy(settings, rating):
    """A timestamped deterministic stand-in; never invokes an external model."""
    baseline = BaselineStrategy(settings.strategy)

    def decide(market, portfolio):
        target = portfolio.btc_quantity * market.price / portfolio.equity_usd
        if rating not in {"Hold", "REVIEW"}:
            target = settings.strategy["rating_target_pct"][rating]
        return replace(
            baseline.decide(market, portfolio),
            rating=rating,
            target_position_pct=None if rating == "REVIEW" else target,
            actionable=rating not in {"Hold", "REVIEW"},
            strategy_version="test-ratings-v1",
            model="test-model",
        )

    return Mock(decide=Mock(side_effect=decide))


def seed_observations(automation, count=MINIMUM_EVALUATION_SAMPLES):
    start = utcnow() - timedelta(days=2)
    with automation.db.connection:
        for index in range(count):
            observed = start + timedelta(seconds=600 * index)
            body = {
                "observed_at": observed,
                "available_at": observed + timedelta(seconds=1),
                "rating": "Sell",
                "bid": "49990",
                "ask": "50010",
                "equity": "10000",
                "config_digest": automation.settings.digest,
                "strategy_version": "test-ratings-v1",
                "model": "test-model",
                "mode": "offline",
                "actionable": True,
            }
            automation.db.connection.execute(
                "INSERT INTO strategy_observations(run_id,body) VALUES (?,?)",
                (f"observation-{index}", dumps(body)),
            )


def test_enable_and_tick_require_explicit_flags(setup):
    _, db, broker, automation = setup
    with pytest.raises(AgentError, match="explicit"):
        automation.enable()
    with pytest.raises(AgentError, match="paused"):
        automation.tick(broker, explicit=True)
    assert automation.enable(explicit=True)["enabled"]
    with pytest.raises(AgentError, match="explicit"):
        automation.tick(broker)
    assert not db.orders()


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"paper": {"trigger": "manual"}}, "scheduled"),
        ({"mode": "paper", "paper": {"trading_enabled": False}}, "explicitly enabled"),
        ({"strategy": {"timeout_seconds": 481}}, "480"),
    ],
)
def test_unsafe_scheduling_configuration_cannot_enable(setup, changes, match):
    settings, db, _, _ = setup
    overrides = dict(changes)
    for name in ("paper", "strategy"):
        if name in overrides:
            overrides[name] = {**getattr(settings, name), **overrides[name]}
    automation = Automation(replace(settings, **overrides), db)
    with pytest.raises(AgentError, match=match):
        automation.enable(explicit=True)
    assert not automation.status()["enabled"]


def test_cadence_survives_restart_and_no_catch_up(setup, monkeypatch):
    settings, db, broker, automation = setup
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    strategy = rating_strategy(settings, "Sell")
    assert automation.tick(broker, explicit=True, strategy=strategy)["status"] == "no_order"
    other_db = Database(settings.database_path, "offline")
    try:
        restarted = Automation(settings, other_db)
        now += timedelta(seconds=599)
        assert restarted.tick(broker, explicit=True, strategy=strategy)["status"] == "cooldown"
        now += timedelta(seconds=1)
        assert restarted.tick(broker, explicit=True, strategy=strategy)["status"] == "no_order"
        now += timedelta(hours=4)
        assert restarted.tick(broker, explicit=True, strategy=strategy)["status"] == "no_order"
        assert restarted.tick(broker, explicit=True, strategy=strategy)["status"] == "cooldown"
        assert other_db.connection.execute("SELECT count(*) FROM auto_cycles").fetchone()[0] == 3
        assert strategy.decide.call_count == 3
        assert not db.orders()
    finally:
        other_db.close()


def test_duplicate_and_overlapping_cycle_cannot_double_submit(setup, tmp_path):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    other_db = Database(settings.database_path, "offline")
    other_broker = OfflineBroker(tmp_path / "broker.sqlite")
    other = Automation(settings, other_db)
    baseline = BaselineStrategy(settings.strategy)
    overlapping_results = []

    def during_analysis(market, portfolio):
        # Independent DB/broker connections contend for the OS cycle lock while
        # the first owner is between reading data and submitting its order.
        overlapping_results.append(other.tick(other_broker, explicit=True))
        return baseline.decide(market, portfolio)

    broker.submit_order = Mock(wraps=broker.submit_order)
    other_broker.submit_order = Mock(wraps=other_broker.submit_order)
    try:
        first = automation.tick(
            broker, explicit=True, strategy=Mock(decide=Mock(side_effect=during_analysis))
        )
        assert first["status"] == "filled", first
        assert overlapping_results[0]["status"] == "busy"
        assert other.tick(other_broker, explicit=True)["status"] == "cooldown"
        assert broker.submit_order.call_count == 1
        other_broker.submit_order.assert_not_called()
        assert db.report()["fill_count"] == 1
    finally:
        other_broker.close()
        other_db.close()


def test_partial_order_skips_then_expires_and_reconciles_after_restart(setup, monkeypatch):
    settings, db, broker, automation = setup
    settings = replace(
        settings,
        strategy={**settings.strategy, "decision_ttl_seconds": 1800},
        risk={**settings.risk, "max_decision_age_seconds": Decimal(1800)},
    )
    automation = Automation(settings, db)
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    broker.fill_fraction = Decimal("0.5")
    broker.submit_order = Mock(wraps=broker.submit_order)
    broker.cancel_order = Mock(wraps=broker.cancel_order)
    first = automation.tick(broker, explicit=True, strategy=BaselineStrategy(settings.strategy))
    assert first["status"] == "partially_filled", first
    original = db.order_for_run(first["run_id"])
    original_quantity = broker.get_portfolio().btc_quantity
    other_db = Database(settings.database_path, "offline")
    try:
        restarted = Automation(settings, other_db)
        hold = rating_strategy(settings, "Hold")
        now += timedelta(seconds=600)
        assert restarted.tick(broker, explicit=True, strategy=hold)["status"] == "pending"
        hold.decide.assert_not_called()
        broker.cancel_order.assert_not_called()
        now += timedelta(seconds=1300)
        final = restarted.tick(broker, explicit=True, strategy=hold)
        assert final["status"] == "no_order", final
        broker.cancel_order.assert_called_once_with(original["client_order_id"])
        assert broker.submit_order.call_count == 1
        assert other_db.order_for_run(first["run_id"])["status"] == "canceled"
        assert broker.get_portfolio().btc_quantity == original_quantity
        assert not broker.get_portfolio().open_orders
        assert other_db.report()["fill_count"] == 1
        assert other_db.report()["ledger_matches_position"]
    finally:
        other_db.close()


@pytest.mark.parametrize("error", [SubmissionUnknown("unknown"), RuntimeError("adapter exploded")])
def test_unknown_submission_immediately_pauses_and_never_retries(setup, error):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    broker.submit_order = Mock(side_effect=error)
    result = automation.tick(broker, explicit=True, strategy=BaselineStrategy(settings.strategy))
    assert db.orders(attempted_only=True)[0]["status"] == "unknown", result
    assert not automation.status()["enabled"], result
    assert automation.pause_path.exists()
    with pytest.raises(AgentError, match="paused"):
        automation.tick(broker, explicit=True)
    assert broker.submit_order.call_count == 1


def test_identity_mismatch_after_acceptance_immediately_pauses(setup):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    submit = broker.submit_order
    broker.submit_order = Mock(side_effect=lambda order: replace(submit(order), client_order_id="wrong-id"))
    result = automation.tick(broker, explicit=True, strategy=BaselineStrategy(settings.strategy))
    assert broker.submit_order.call_count == 1
    assert db.orders(attempted_only=True)[0]["status"] == "unknown"
    assert not automation.status()["enabled"], result


def test_accepted_then_timeout_recovers_without_duplicate_or_pause(setup):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    submit = broker.submit_order

    def accept_then_timeout(order):
        submit(order)
        raise SubmissionUnknown("lost response")

    broker.submit_order = Mock(side_effect=accept_then_timeout)
    result = automation.tick(broker, explicit=True, strategy=BaselineStrategy(settings.strategy))
    assert result["status"] == "filled", result
    assert result["execution"]["execution"]["recovered_after_uncertainty"]
    assert automation.status()["enabled"]
    assert automation.tick(broker, explicit=True)["status"] == "cooldown"
    assert broker.submit_order.call_count == 1
    assert db.report()["fill_count"] == 1


def test_pause_during_analysis_prevents_submit_without_waiting_for_database(setup):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    baseline = BaselineStrategy(settings.strategy)
    broker.submit_order = Mock(wraps=broker.submit_order)

    def pause_while_database_is_locked(market, portfolio):
        assert automation.pause("stop during slow model analysis")["status"] == "paused"
        assert automation.pause_path.exists()
        return baseline.decide(market, portfolio)

    result = automation.tick(
        broker, explicit=True, strategy=Mock(decide=Mock(side_effect=pause_while_database_is_locked))
    )
    assert result["status"] == "failed"
    broker.submit_order.assert_not_called()
    assert not db.orders(attempted_only=True)
    assert not automation.status()["enabled"]


def test_pause_immediately_before_post_is_rechecked(setup):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    broker.submit_order = Mock(wraps=broker.submit_order)
    lookup = broker.get_order

    def pause_at_last_pre_post_lookup(client_order_id):
        result = lookup(client_order_id)
        if result is None:
            automation.pause("pause between execution validation and POST")
        return result

    broker.get_order = pause_at_last_pre_post_lookup
    automation.tick(broker, explicit=True, strategy=BaselineStrategy(settings.strategy))
    broker.submit_order.assert_not_called()
    assert not broker.get_portfolio().positions
    assert not automation.status()["enabled"]


def test_changed_configuration_invalidates_approval_before_broker_access(setup):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    settings = replace(settings, risk={**settings.risk, "max_order_notional_usd": Decimal(1501)})
    changed = Automation(settings, db)
    broker.get_portfolio = Mock(side_effect=AssertionError("must not reach broker"))
    with pytest.raises(AgentError, match="configuration differs"):
        changed.tick(broker, explicit=True)
    broker.get_portfolio.assert_not_called()
    assert changed.pause_path.exists()


def test_new_policy_digest_does_not_reuse_old_optimization_observations(setup):
    settings, db, _, automation = setup
    seed_observations(automation, 3)
    changed = replace(settings, risk={**settings.risk, "max_order_notional_usd": Decimal(1400)})
    replacement = Automation(changed, db)
    status = replacement.enable(explicit=True)
    assert status["state"]["evaluation_cursor"] == 3
    assert status["state"]["evaluation_cursors"]["BTC/USD"] == 3
    assert status["observation_count_by_symbol"]["BTC/USD"] == 0


@pytest.mark.parametrize(
    "url", ["https://api.alpaca.markets", "https://paper-api.alpaca.markets.attacker.test"]
)
def test_automatic_explicit_execution_never_bypasses_paper_origin(url):
    with pytest.raises(BrokerError, match="live trading is disabled"):
        AlpacaPaperBroker(url, "synthetic-key", "synthetic-secret", allow_submit=True)


def test_three_consecutive_cycle_failures_persistently_halt(setup, monkeypatch):
    _, db, broker, automation = setup
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    broker.get_market = Mock(side_effect=BrokerError("read-only connection failed"))
    for index in range(3):
        result = automation.tick(broker, explicit=True)
        assert result["status"] == "failed"
        assert automation.status()["state"]["failure_count"] == index + 1
        assert automation.status()["enabled"] is (index < 2)
        now += timedelta(seconds=600)
    assert result["automatic_paused"]
    assert automation.pause_path.exists()
    assert db.connection.execute("SELECT count(*) FROM auto_cycles").fetchone()[0] == 3


def test_daily_loss_immediately_halts_automatic_execution(setup):
    _, db, broker, automation = setup
    automation.enable(explicit=True)
    original = broker.get_portfolio()
    db.snapshot(original, broker.get_market())
    broker.get_portfolio = Mock(
        return_value=replace(original, equity_usd=Decimal("9800"), cash_usd=Decimal("9800"))
    )
    broker.submit_order = Mock(side_effect=AssertionError("loss limit must prevent submission"))
    result = automation.tick(broker, explicit=True)
    assert result["status"] == "halted"
    assert result["automatic_paused"]
    assert not automation.status()["enabled"]
    assert "loss limit" in automation._state()["halt_reason"]
    broker.submit_order.assert_not_called()


@pytest.mark.parametrize("cancel_timeout", [False, True])
def test_daily_loss_with_partial_order_halts_and_cancels_remaining_quantity(
    setup, monkeypatch, cancel_timeout
):
    settings, db, broker, _ = setup
    settings = replace(
        settings,
        strategy={**settings.strategy, "decision_ttl_seconds": 1800},
        risk={**settings.risk, "max_decision_age_seconds": Decimal(1800)},
    )
    automation = Automation(settings, db)
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    broker.fill_fraction = Decimal("0.5")
    first = automation.tick(broker, explicit=True, strategy=BaselineStrategy(settings.strategy))
    assert first["status"] == "partially_filled"
    cid = db.order_for_run(first["run_id"])["client_order_id"]
    get_portfolio = broker.get_portfolio
    broker.get_portfolio = Mock(side_effect=lambda: replace(get_portfolio(), equity_usd=Decimal("9800")))
    broker.submit_order = Mock(side_effect=AssertionError("no new exposure after loss"))
    broker.cancel_order = (
        Mock(side_effect=TimeoutError("uncertain cancellation"))
        if cancel_timeout
        else Mock(wraps=broker.cancel_order)
    )
    strategy = Mock(decide=Mock(side_effect=AssertionError("must halt before analysis")))
    now += timedelta(seconds=600)
    result = automation.tick(broker, explicit=True, strategy=strategy)
    assert result["status"] == "halted"
    assert result["reason"] == "daily_loss_limit"
    assert not automation.status()["enabled"]
    broker.cancel_order.assert_called_once_with(cid)
    assert result["cancellations"][0]["status"] == (
        "reconciliation_required" if cancel_timeout else "canceled"
    )
    broker.submit_order.assert_not_called()
    strategy.decide.assert_not_called()


@pytest.mark.parametrize("rating", ["Sell", "Hold", "REVIEW"])
def test_no_order_ratings_record_actual_availability_and_provenance(setup, rating):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    before = utcnow()
    result = automation.tick(broker, explicit=True, strategy=rating_strategy(settings, rating))
    after = utcnow()
    assert result["status"] == "no_order", result
    rows = db.connection.execute("SELECT body FROM strategy_observations").fetchall()
    assert len(rows) == 1
    observation = json.loads(rows[0][0])
    assert observation["rating"] == rating
    assert before <= timestamp(observation["available_at"]) <= after
    assert timestamp(observation["observed_at"]) <= timestamp(observation["available_at"])
    assert observation["config_digest"] == settings.digest
    assert observation["strategy_version"] == "test-ratings-v1"
    assert observation["model"] == "test-model"
    assert observation["mode"] == "offline"
    assert observation["actionable"] is (rating != "REVIEW")
    assert not db.orders()


def test_insufficient_evidence_reports_diagnostics_without_changing_policy(setup, monkeypatch):
    _, _, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation, MINIMUM_EVALUATION_SAMPLES - 1)
    state = automation._state()
    result = automation._evaluate(broker.get_asset_rules())
    assert result["status"] == "insufficient_evidence"
    assert automation._state() == state
    assert result["holdout_evaluated"] is False
    assert result["diagnostics"]["scope"] == "incumbent_training_prefix_only"


@pytest.mark.parametrize("candidate", ["1.01", "2", "0.6"])
def test_evaluator_cannot_expand_or_invent_exposure_multiplier(setup, monkeypatch, candidate):
    settings, _, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    monkeypatch.setattr(
        "crypto_agent.evaluation.evaluate_candidates",
        Mock(return_value={"status": "promote", "recommended_multiplier": candidate}),
    )
    original_state, original_risk = automation._state(), dict(settings.risk)
    with pytest.raises(AgentError, match="forbidden"):
        automation._evaluate(broker.get_asset_rules())
    assert automation._state() == original_state
    assert settings.risk == original_risk


def test_evaluator_cannot_invent_observation_count(setup, monkeypatch):
    _, _, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    monkeypatch.setattr(
        "crypto_agent.evaluation.evaluate_candidates",
        Mock(
            return_value={
                "status": "reject",
                "recommended_multiplier": "1",
                "holdout_evaluated": True,
                "observation_count": MINIMUM_EVALUATION_SAMPLES + 1,
            }
        ),
    )
    before = automation._state()
    with pytest.raises(AgentError, match="invalid observation count"):
        automation._evaluate(broker.get_asset_rules())
    assert automation._state() == before


def test_evaluator_cannot_restore_exposure_after_a_reduction(setup, monkeypatch):
    _, _, broker, automation = setup
    automation.enable(explicit=True)
    state = automation._state()
    state["current_multiplier"] = "0.5"
    automation._save(state)
    seed_observations(automation)
    monkeypatch.setattr(
        "crypto_agent.evaluation.evaluate_candidates",
        Mock(return_value={"status": "promote", "recommended_multiplier": "0.75", "holdout_evaluated": True}),
    )
    with pytest.raises(AgentError, match="forbidden"):
        automation._evaluate(broker.get_asset_rules())
    assert automation._state()["current_multiplier"] == "0.5"


def test_evaluator_cannot_mutate_original_risk_or_rating_targets(setup, monkeypatch):
    settings, _, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    original_risk, original_targets = dict(settings.risk), automation._state()["base_targets"]

    def hostile_evaluator(observations, targets, risk, **kwargs):
        risk["max_order_notional_usd"] = Decimal("1000000000")
        targets["Buy"] = Decimal("1")
        return {"status": "promote", "recommended_multiplier": "0.75", "holdout_evaluated": True}

    monkeypatch.setattr("crypto_agent.evaluation.evaluate_candidates", hostile_evaluator)
    automation._evaluate(broker.get_asset_rules())
    assert settings.risk == original_risk
    assert automation._state()["base_targets"] == original_targets
    assert automation._effective_settings().risk == original_risk
    assert automation._effective_settings().strategy["rating_target_pct"]["Buy"] == (
        settings.strategy["rating_target_pct"]["Buy"] * Decimal("0.75")
    )


@pytest.mark.parametrize("result_status", ["promote", "reject"])
def test_holdout_cursor_persists_and_each_window_is_consumed_once(setup, monkeypatch, result_status):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    evaluator = Mock(
        return_value={"status": result_status, "recommended_multiplier": "0.75", "holdout_evaluated": True}
    )
    monkeypatch.setattr("crypto_agent.evaluation.evaluate_candidates", evaluator)
    assert automation._evaluate(broker.get_asset_rules())["status"] == result_status
    assert automation._state()["evaluation_cursor"] == MINIMUM_EVALUATION_SAMPLES
    assert automation._state()["current_multiplier"] == ("0.75" if result_status == "promote" else "1")
    other_db = Database(settings.database_path, "offline")
    try:
        restarted = Automation(settings, other_db)
        result = restarted._evaluate(broker.get_asset_rules())
        assert result["status"] == "insufficient_evidence"
        assert result["new_samples"] == 0
        assert evaluator.call_count == 1
        assert db.connection.execute("SELECT count(*) FROM strategy_evaluations").fetchone()[0] == 1
    finally:
        other_db.close()


def test_policy_promotion_and_audit_commit_atomically(setup, monkeypatch):
    _, db, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    original = automation._state()
    monkeypatch.setattr(
        "crypto_agent.evaluation.evaluate_candidates",
        Mock(return_value={"status": "promote", "recommended_multiplier": "0.75", "holdout_evaluated": True}),
    )
    db.connection.execute("""
        CREATE TRIGGER fail_policy_write BEFORE INSERT ON metadata
        WHEN NEW.key = 'automatic_policy'
        BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="simulated storage failure"):
        automation._evaluate(broker.get_asset_rules())
    assert automation._state() == original
    assert db.connection.execute("SELECT count(*) FROM strategy_evaluations").fetchone()[0] == 0


def test_invalid_review_is_not_optimization_evidence(setup):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    strategy = rating_strategy(settings, "REVIEW")
    original = strategy.decide.side_effect
    strategy.decide.side_effect = lambda market, portfolio: replace(
        original(market, portfolio), evaluation_eligible=False
    )
    result = automation.tick(broker, explicit=True, strategy=strategy)
    assert result["status"] == "no_order"
    assert db.connection.execute("SELECT count(*) FROM strategy_observations").fetchone()[0] == 0


def test_optimization_multiplier_scales_probe_cap_and_budget(setup):
    settings, db, _, _ = setup
    config = {
        **settings.strategy,
        "intraday_entry_policy": "capped_probe",
        "intraday_probe_max_position_usd": Decimal(10),
        "intraday_probe_cost_budget_usd": Decimal(".15"),
    }
    automation = Automation(replace(settings, strategy=config), db)
    automation.enable(explicit=True)
    state = automation._state()
    state["current_multiplier"] = "0.5"
    automation._save(state)
    effective = automation._effective_settings()
    assert effective.strategy["intraday_probe_max_position_usd"] == Decimal(5)
    assert effective.strategy["intraday_probe_cost_budget_usd"] == Decimal(".075")
    assert effective.risk == settings.risk


@pytest.mark.parametrize("result_status", ["insufficient_evidence", "invalid_evidence"])
def test_unexamined_holdout_is_retained_across_restart(setup, monkeypatch, result_status):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    before = automation._state()
    evaluator = Mock(return_value={"status": result_status, "holdout_evaluated": False})
    monkeypatch.setattr("crypto_agent.evaluation.evaluate_candidates", evaluator)
    automation._evaluate(broker.get_asset_rules())
    assert automation._state() == before
    other_db = Database(settings.database_path, "offline")
    try:
        restarted = Automation(settings, other_db)
        restarted._evaluate(broker.get_asset_rules())
        assert evaluator.call_count == 2
        assert len(evaluator.call_args.args[0]) == MINIMUM_EVALUATION_SAMPLES
        assert db.connection.execute("SELECT count(*) FROM strategy_evaluations").fetchone()[0] == 2
    finally:
        other_db.close()


def test_stale_historical_window_is_not_evaluated_as_current_cycle_evidence(setup, monkeypatch):
    _, db, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    state = automation._state()
    state["last_started_at"] = now.isoformat()
    automation._save(state)
    evaluator = Mock(side_effect=AssertionError("stale evidence must not reach evaluator"))
    monkeypatch.setattr("crypto_agent.evaluation.evaluate_candidates", evaluator)
    before = automation._state()
    result = automation._evaluate(broker.get_asset_rules())
    assert result["status"] == "insufficient_evidence"
    assert result["holdout_evaluated"] is False
    assert "is stale" in result["reason"]
    assert automation._state() == before
    now += timedelta(seconds=automation.evaluation_gap_seconds)
    state = automation._state()
    state["last_started_at"] = now.isoformat()
    automation._save(state)
    result = automation._evaluate(broker.get_asset_rules())
    assert result["status"] == "insufficient_evidence"
    assert "is stale" in result["reason"]
    assert automation._state() == state
    evaluator.assert_not_called()
    assert db.connection.execute("SELECT count(*) FROM strategy_evaluations").fetchone()[0] == 0


def test_status_marks_historical_evaluation_stale_without_mutating_audit(setup, monkeypatch):
    _, db, broker, automation = setup
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    seed_observations(automation)
    rows = db.connection.execute("SELECT id,body FROM strategy_observations ORDER BY id").fetchall()
    latest_available = now - timedelta(seconds=60)
    first_observed = latest_available - timedelta(seconds=600 * (len(rows) - 1) + 1)
    with db.connection:
        for index, row in enumerate(rows):
            body = json.loads(row["body"])
            observed = first_observed + timedelta(seconds=600 * index)
            body["observed_at"] = observed
            body["available_at"] = observed + timedelta(seconds=1)
            db.connection.execute(
                "UPDATE strategy_observations SET body=? WHERE id=?", (dumps(body), row["id"])
            )
    result = automation._evaluate(broker.get_asset_rules())
    assert result["holdout_evaluated"] is True
    state = automation._state()
    state["last_started_at"] = (now - timedelta(seconds=300)).isoformat()
    automation._save(state)
    status = automation.status()
    audit = status["last_evaluation"]
    assert audit["audit"]["evaluated_at"]
    assert audit["selected_last_observation_id"] == MINIMUM_EVALUATION_SAMPLES
    assert audit["current_status"]["status"] == "current"
    assert status["evaluation_diagnostics_by_symbol"]["BTC/USD"]["status"] == "current"
    saved = automation._state()
    now += timedelta(seconds=automation.evaluation_gap_seconds + 1)
    stale = automation.status()
    assert stale["state"] == saved
    assert stale["last_evaluation"]["selected_last_observation_id"] == audit["selected_last_observation_id"]
    assert stale["last_evaluation"]["current_status"]["status"] == "insufficient_evidence"
    assert "is stale" in stale["last_evaluation"]["current_status"]["reason"]
    assert stale["evaluation_diagnostics_by_symbol"]["BTC/USD"]["raw_new_observations"] == 0
    assert stale["evaluation_diagnostics_by_symbol"]["BTC/USD"]["current_config_observations"] == 0


def test_status_marks_future_evidence_invalid_without_cycle_started(setup, monkeypatch):
    _, db, _, automation = setup
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    seed_observations(automation, 1)
    row = db.connection.execute("SELECT id,body FROM strategy_observations").fetchone()
    body = json.loads(row["body"])
    body["available_at"] = now + timedelta(seconds=1)
    with db.connection:
        db.connection.execute("UPDATE strategy_observations SET body=? WHERE id=?", (dumps(body), row["id"]))
    status = automation.status()
    diagnostic = status["evaluation_diagnostics_by_symbol"]["BTC/USD"]
    assert diagnostic["status"] == "invalid_evidence"
    assert "future" in diagnostic["reason"]


def test_146_points_one_second_short_then_append_reaches_readiness(setup):
    _, db, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    rows = db.connection.execute("SELECT id,body FROM strategy_observations ORDER BY id").fetchall()
    with db.connection:
        for index, row in enumerate(rows):
            body = json.loads(row["body"])
            body["cycle_started_at"] = body["observed_at"]
            if index == 0:
                body["observed_at"] = timestamp(body["observed_at"]) + timedelta(seconds=1)
            db.connection.execute(
                "UPDATE strategy_observations SET body=? WHERE id=?", (dumps(body), row["id"])
            )
    before = automation._state()
    result = automation._evaluate(broker.get_asset_rules())
    assert result["holdout_evaluated"] is False
    assert result["training_seconds"] == 57599
    assert automation._state() == before
    body = json.loads(rows[-1]["body"])
    for key in ("observed_at", "available_at"):
        body[key] = timestamp(body[key]) + timedelta(seconds=600)
    body["cycle_started_at"] = body["observed_at"]
    with db.connection:
        db.connection.execute(
            "INSERT INTO strategy_observations(run_id,body) VALUES (?,?)", ("additional-point", dumps(body))
        )
    result = automation._evaluate(broker.get_asset_rules())
    assert result["holdout_evaluated"] is True
    assert automation._state()["evaluation_cursor"] == 147


def test_promotion_requires_explicit_examined_holdout(setup, monkeypatch):
    _, _, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    monkeypatch.setattr(
        "crypto_agent.evaluation.evaluate_candidates",
        Mock(return_value={"status": "promote", "recommended_multiplier": "0.75"}),
    )
    with pytest.raises(AgentError, match="examined holdout"):
        automation._evaluate(broker.get_asset_rules())
    assert automation._state()["current_multiplier"] == "1"


@pytest.mark.parametrize("reason", sorted(automation_module.ORDINARY_PRE_SUBMIT_REASONS))
def test_repeated_exact_pre_submit_blocks_skip_without_halting(setup, monkeypatch, reason):
    from crypto_agent.models import RiskResult

    settings, db, broker, automation = setup
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    broker.submit_order = Mock(side_effect=AssertionError("must not submit"))
    monkeypatch.setattr(
        automation_module,
        "execute_preview",
        lambda *args, **kwargs: {"status": "blocked", "risk": RiskResult(False, (reason,))},
    )
    for _ in range(4):
        result = automation.tick(broker, explicit=True, strategy=rating_strategy(settings, "Buy"))
        assert result["status"] == "blocked", result
        assert result["ordinary_pre_submit_skip"] is True
        assert automation._state()["failure_count"] == 0
        assert automation.status()["enabled"] is True
        now += timedelta(seconds=600)
    assert not db.orders(attempted_only=True)
    broker.submit_order.assert_not_called()


def test_unknown_risk_block_still_counts_as_failure(setup, monkeypatch):
    from crypto_agent.models import RiskResult

    settings, _, broker, automation = setup
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    monkeypatch.setattr(
        automation_module,
        "execute_preview",
        lambda *args, **kwargs: {
            "status": "blocked",
            "risk": RiskResult(False, ("Unrecognized safety failure",)),
        },
    )
    for index in range(3):
        result = automation.tick(broker, explicit=True, strategy=rating_strategy(settings, "Buy"))
        assert automation._state()["failure_count"] == index + 1
        now += timedelta(seconds=600)
    assert result["automatic_paused"] is True


@pytest.mark.parametrize("field,value", [("config_digest", "unapproved"), ("mode", "paper")])
def test_latest_evidence_must_match_approved_configuration_and_mode(setup, monkeypatch, field, value):
    _, db, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation)
    with db.connection:
        for row in db.connection.execute("SELECT id,body FROM strategy_observations").fetchall():
            body = json.loads(row["body"])
            body[field] = value
            db.connection.execute(
                "UPDATE strategy_observations SET body=? WHERE id=?", (dumps(body), row["id"])
            )
    evaluator = Mock(side_effect=AssertionError("unapproved evidence"))
    monkeypatch.setattr("crypto_agent.evaluation.evaluate_candidates", evaluator)
    before = automation._state()
    result = automation._evaluate(broker.get_asset_rules())
    assert result["status"] == "invalid_evidence"
    assert result["holdout_evaluated"] is False
    assert automation._state() == before
    evaluator.assert_not_called()


def test_selected_suffix_audit_ids_exclude_old_prefix(setup):
    _, db, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation, 166)
    with db.connection:
        for row in db.connection.execute("SELECT id,body FROM strategy_observations WHERE id<=20").fetchall():
            body = json.loads(row["body"])
            body["model"] = "old-model"
            db.connection.execute(
                "UPDATE strategy_observations SET body=? WHERE id=?", (dumps(body), row["id"])
            )
    result = automation._evaluate(broker.get_asset_rules())
    assert result["excluded_prefix_count"] == 20
    assert result["new_samples"] == 146
    assert result["total_new_samples"] == 166
    assert result["selected_first_observation_id"] == 21
    assert result["selected_last_observation_id"] == 166
    row = db.connection.execute(
        "SELECT first_observation_id,last_observation_id FROM strategy_evaluations"
    ).fetchone()
    assert tuple(row) == (21, 166)
    assert automation._state()["evaluation_cursor"] == 166


@pytest.mark.parametrize("reason", sorted(automation_module.ORDINARY_PRE_SUBMIT_REASONS))
@pytest.mark.parametrize("attempted", [False, True])
def test_quote_block_exception_only_skips_when_proven_unsubmitted(setup, monkeypatch, attempted, reason):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)

    def blocked(settings, guarded, database, run_id, **kwargs):
        if attempted:
            database.claim_submission(database.order_for_run(run_id)["client_order_id"])
        raise AgentError(reason)

    monkeypatch.setattr(automation_module, "execute_preview", blocked)
    result = automation.tick(broker, explicit=True, strategy=rating_strategy(settings, "Buy"))
    if attempted:
        assert result["status"] == "halted"
        assert result["reason"] == "unknown_order"
        assert not automation.status()["enabled"]
        assert "ordinary_pre_submit_skip" not in result
    else:
        assert result["status"] == "blocked"
        assert result["ordinary_pre_submit_skip"] is True
        assert automation._state()["failure_count"] == 0
        assert not db.orders(attempted_only=True)


@pytest.mark.parametrize("reason", sorted(automation_module.ORDINARY_PRE_SUBMIT_REASONS))
@pytest.mark.parametrize("attempted", [False, True])
def test_quote_reason_cannot_hide_submitted_or_mixed_risk_failure(setup, monkeypatch, attempted, reason):
    from crypto_agent.models import RiskResult

    settings, _, broker, automation = setup
    automation.enable(explicit=True)

    def blocked(settings, guarded, database, run_id, **kwargs):
        reasons = [reason]
        if attempted:
            row = database.order_for_run(run_id)
            database.claim_submission(row["client_order_id"])
            database.order_state(row["client_order_id"], "rejected")
        else:
            reasons.append("Daily loss limit reached")
        return {"status": "blocked", "risk": RiskResult(False, tuple(reasons))}

    monkeypatch.setattr(automation_module, "execute_preview", blocked)
    result = automation.tick(broker, explicit=True, strategy=rating_strategy(settings, "Buy"))
    assert "ordinary_pre_submit_skip" not in result
    assert automation._state()["failure_count"] == 1


def test_automation_cannot_bypass_146_gate_with_early_evaluator_output(setup, monkeypatch):
    _, _, broker, automation = setup
    automation.enable(explicit=True)
    seed_observations(automation, 145)
    monkeypatch.setattr(
        "crypto_agent.evaluation.evaluate_candidates",
        Mock(return_value={"status": "promote", "holdout_evaluated": True, "recommended_multiplier": "0.75"}),
    )
    before = automation._state()
    with pytest.raises(AgentError, match="sample gate"):
        automation._evaluate(broker.get_asset_rules())
    assert automation._state() == before


@pytest.fixture
def dual_setup(setup):
    """Actual offline BTC execution plus synthetic XRP quotes for serial decisions."""
    settings, db, broker, _ = setup
    settings = replace(
        settings,
        paper={
            **settings.paper,
            "symbols": ["BTC/USD", "XRP/USD"],
            "automatic_interval_seconds": 300,
            "automatic_symbols_per_cycle": 2,
        },
        risk={
            **settings.risk,
            "allowed_symbols": ["BTC/USD", "XRP/USD"],
            "min_price_usd": Decimal(".001"),
            "price_bounds_usd": {
                **settings.risk["price_bounds_usd"],
                "XRP/USD": {"min": Decimal(".001"), "max": Decimal(100)},
            },
        },
    )
    original_market = broker.get_market
    original_rules = broker.get_asset_rules
    broker.symbols = tuple(settings.paper["symbols"])

    def market(symbol="BTC/USD"):
        quote = original_market()
        if symbol == "BTC/USD":
            return quote
        return replace(quote, symbol=symbol, price=Decimal(2), bid=Decimal("1.999"), ask=Decimal("2.001"))

    broker.get_market = market
    broker.get_markets = lambda symbols: {symbol: market(symbol) for symbol in symbols}
    broker.get_asset_rules = lambda symbol="BTC/USD": replace(original_rules(), symbol=symbol)
    automation = Automation(settings, db)
    baseline = BaselineStrategy(settings.strategy)

    def decide(market, portfolio):
        return replace(
            baseline.decide(market, portfolio),
            rating="Sell",
            target_position_pct=Decimal(0),
            strategy_version="dual-fixture-v1",
            model="dual-fixture-model",
        )

    yield settings, db, broker, automation, Mock(decide=Mock(side_effect=decide))


@pytest.mark.parametrize("interval", [60, 120, 300, 600, 3600])
def test_dual_round_claims_one_slot_and_preserves_cadence_after_restart(dual_setup, monkeypatch, interval):
    settings, db, broker, _, strategy = dual_setup
    settings = replace(settings, paper={**settings.paper, "automatic_interval_seconds": interval})
    automation = Automation(settings, db)
    assert automation.enable(explicit=True)["interval_seconds"] == interval
    result = automation.tick(broker, explicit=True, strategy=strategy)
    now = timestamp(automation._state()["last_started_at"])
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    assert result["status"] == "no_order", result
    assert [item["symbol"] for item in result["results"]] == ["BTC/USD", "XRP/USD"]
    assert result["skipped_symbols"] == []
    assert all("optimization" in item for item in result["results"])
    observations = [
        json.loads(row[0]) for row in db.connection.execute("SELECT body FROM strategy_observations")
    ]
    assert [item["symbol"] for item in observations] == ["BTC/USD", "XRP/USD"]
    assert len({item["cycle_started_at"] for item in observations}) == 1
    other_db = Database(settings.database_path, "offline")
    try:
        restarted = Automation(settings, other_db)
        now += timedelta(seconds=interval - 1)
        assert restarted.tick(broker, explicit=True, strategy=strategy)["status"] == "cooldown"
        assert strategy.decide.call_count == 2
        now += timedelta(seconds=1)
        assert restarted.tick(broker, explicit=True, strategy=strategy)["status"] == "no_order"
        assert strategy.decide.call_count == 4
        assert db.connection.execute("SELECT count(*) FROM auto_cycles").fetchone()[0] == 2
        assert restarted.tick(broker, explicit=True, strategy=strategy)["status"] == "cooldown"
    finally:
        other_db.close()


def test_cycle_lock_covers_both_symbols_even_when_round_exceeds_interval(dual_setup, monkeypatch):
    settings, db, broker, automation, strategy = dual_setup
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    other_db = Database(settings.database_path, "offline")
    other = Automation(settings, other_db)
    decide = strategy.decide.side_effect
    overlaps = []

    def while_analyzing(market, portfolio):
        nonlocal now
        now += timedelta(seconds=301)
        overlaps.append(other.tick(broker, explicit=True))
        return decide(market, portfolio)

    strategy.decide.side_effect = while_analyzing
    try:
        result = automation.tick(broker, explicit=True, strategy=strategy)
        assert result["status"] == "no_order", result
        assert [item["status"] for item in overlaps] == ["busy", "busy"]
        assert db.connection.execute("SELECT count(*) FROM auto_cycles").fetchone()[0] == 1
    finally:
        other_db.close()


def test_second_symbol_reconciles_fresh_account_after_first_fill(dual_setup, monkeypatch):
    settings, db, broker, automation, strategy = dual_setup
    automation.enable(explicit=True)
    baseline = BaselineStrategy(settings.strategy)
    sell = strategy.decide.side_effect
    reconciliations = []
    reconcile = automation_module.reconcile_account

    def capture_reconciliation(*args):
        result = reconcile(*args)
        reconciliations.append(result["portfolio"].btc_quantity)
        return result

    monkeypatch.setattr(automation_module, "reconcile_account", capture_reconciliation)

    def buy_then_inspect(market, portfolio):
        if market.symbol == "BTC/USD":
            return baseline.decide(market, portfolio)
        assert portfolio.btc_quantity > 0
        assert portfolio.cash_usd < Decimal(10000)
        assert reconciliations[-1] == portfolio.btc_quantity
        assert db.report()["fill_count"] == 1
        return sell(market, portfolio)

    strategy.decide.side_effect = buy_then_inspect
    result = automation.tick(broker, explicit=True, strategy=strategy)
    assert result["status"] == "filled", result
    assert [item["status"] for item in result["results"]] == ["filled", "no_order"]
    assert len(reconciliations) == 4
    assert db.report()["ledger_matches_position"]


@pytest.mark.parametrize("outcome", ["partial", "unknown", "fault", "blocked", "pause"])
def test_first_symbol_stops_remaining_round_on_unfinished_or_unsafe_outcome(dual_setup, monkeypatch, outcome):
    settings, db, broker, automation, strategy = dual_setup
    automation.enable(explicit=True)
    baseline = BaselineStrategy(settings.strategy)
    strategy.decide.side_effect = baseline.decide
    broker.submit_order = Mock(wraps=broker.submit_order)
    if outcome == "partial":
        broker.fill_fraction = Decimal(".5")
    elif outcome == "unknown":
        broker.submit_order.side_effect = SubmissionUnknown("lost submission response")
    elif outcome == "fault":
        strategy.decide.side_effect = BrokerError("synthetic analysis failure")
    elif outcome == "blocked":
        monkeypatch.setattr(
            automation_module,
            "execute_preview",
            Mock(side_effect=AgentError("Price moved beyond preview tolerance; create a new preview")),
        )
    else:

        def pause_during_analysis(market, portfolio):
            automation.pause("stop during first symbol")
            return baseline.decide(market, portfolio)

        strategy.decide.side_effect = pause_during_analysis
    result = automation.tick(broker, explicit=True, strategy=strategy)
    assert len(result["results"]) == 1, result
    assert result["skipped_symbols"] == ["XRP/USD"]
    assert strategy.decide.call_count == 1
    assert db.connection.execute("SELECT count(*) FROM auto_cycles").fetchone()[0] == 1
    assert automation._state()["failure_count"] <= 1
    if outcome in {"fault", "blocked", "pause"}:
        broker.submit_order.assert_not_called()
    if outcome == "blocked":
        assert result["results"][0]["ordinary_pre_submit_skip"]
        assert automation._state()["failure_count"] == 0
    if outcome in {"unknown", "pause"}:
        assert not automation.status()["enabled"]
        assert result["automatic_paused"]


def test_pause_between_symbols_prevents_second_analysis(dual_setup, monkeypatch):
    _, _, broker, automation, strategy = dual_setup
    automation.enable(explicit=True)
    evaluate = automation._evaluate

    def evaluate_then_pause(asset):
        result = evaluate(asset)
        automation.pause("stop between symbols")
        return result

    monkeypatch.setattr(automation, "_evaluate", evaluate_then_pause)
    result = automation.tick(broker, explicit=True, strategy=strategy)
    assert result["automatic_paused"]
    assert result["skipped_symbols"] == ["XRP/USD"]
    assert strategy.decide.call_count == 1


def test_second_symbol_failure_counts_once_per_round_and_halts_after_three(dual_setup, monkeypatch):
    _, _, broker, automation, strategy = dual_setup
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    decide = strategy.decide.side_effect

    def fail_second(market, portfolio):
        if market.symbol == "XRP/USD":
            raise BrokerError("synthetic second-symbol fault")
        return decide(market, portfolio)

    strategy.decide.side_effect = fail_second
    automation.enable(explicit=True)
    for index in range(3):
        result = automation.tick(broker, explicit=True, strategy=strategy)
        assert result["status"] == "failed", result
        assert len(result["results"]) == 2
        assert automation._state()["failure_count"] == index + 1
        assert automation.status()["enabled"] is (index < 2)
        now += timedelta(seconds=300)
    assert result["automatic_paused"]


def test_two_symbol_groups_rotate_durably_when_universe_has_three(dual_setup, monkeypatch):
    settings, db, broker, _, strategy = dual_setup
    settings = replace(
        settings,
        paper={**settings.paper, "symbols": ["BTC/USD", "XRP/USD", "ETH/USD"]},
        risk={
            **settings.risk,
            "allowed_symbols": ["BTC/USD", "XRP/USD", "ETH/USD"],
            "price_bounds_usd": {
                **settings.risk["price_bounds_usd"],
                "ETH/USD": {"min": Decimal(".001"), "max": Decimal(100)},
            },
        },
    )
    broker.symbols = tuple(settings.paper["symbols"])
    automation = Automation(settings, db)
    automation.enable(explicit=True)
    first = automation.tick(broker, explicit=True, strategy=strategy)
    assert [item["symbol"] for item in first["results"]] == ["BTC/USD", "XRP/USD"]
    now = timestamp(automation._state()["last_started_at"]) + timedelta(seconds=300)
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    other_db = Database(settings.database_path, "offline")
    try:
        restarted = Automation(settings, other_db)
        second = restarted.tick(broker, explicit=True, strategy=strategy)
        assert [item["symbol"] for item in second["results"]] == ["ETH/USD", "BTC/USD"]
        assert restarted._state()["next_symbol_index"] == 1
        now += timedelta(seconds=300)
        third = restarted.tick(broker, explicit=True, strategy=strategy)
        assert [item["symbol"] for item in third["results"]] == ["XRP/USD", "ETH/USD"]
        assert restarted._state()["next_symbol_index"] == 0
    finally:
        other_db.close()


def test_existing_pending_order_stops_entire_dual_round_after_restart(dual_setup, monkeypatch):
    settings, db, broker, _, strategy = dual_setup
    settings = replace(
        settings,
        strategy={**settings.strategy, "decision_ttl_seconds": 1800},
        risk={**settings.risk, "max_decision_age_seconds": Decimal(1800)},
    )
    automation = Automation(settings, db)
    automation.enable(explicit=True)
    broker.fill_fraction = Decimal(".5")
    first = automation.tick(broker, explicit=True, strategy=BaselineStrategy(settings.strategy))
    assert first["status"] == "partially_filled", first
    now = timestamp(automation._state()["last_started_at"]) + timedelta(seconds=300)
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    other_db = Database(settings.database_path, "offline")
    try:
        restarted = Automation(settings, other_db)
        result = restarted.tick(broker, explicit=True, strategy=strategy)
        assert result["status"] == "pending", result
        assert result["skipped_symbols"] == ["XRP/USD"]
        strategy.decide.assert_not_called()
        assert len(other_db.orders(attempted_only=True)) == 1
        assert restarted._state()["failure_count"] == 0
    finally:
        other_db.close()


@pytest.mark.parametrize(
    "reason",
    [
        "Preview sizing changed with account/market; create a new preview",
        "Buy does not match the decision target",
        "Sell does not match the decision target",
    ],
)
@pytest.mark.parametrize("attempted", [False, True])
def test_three_refresh_sizing_exceptions_skip_only_before_submission(setup, monkeypatch, reason, attempted):
    settings, db, broker, automation = setup
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    automation.enable(explicit=True)
    broker.submit_order = Mock(side_effect=AssertionError("test must not submit"))

    def raise_sizing_error(settings, guarded, database, run_id, **kwargs):
        if attempted:
            row = database.order_for_run(run_id)
            database.claim_submission(row["client_order_id"])
            # A known terminal rejection allows the next round but remains a
            # true attempted-order fault, regardless of its exception wording.
            database.order_state(row["client_order_id"], "rejected")
        raise AgentError(reason)

    monkeypatch.setattr(automation_module, "execute_preview", raise_sizing_error)
    for index in range(3):
        result = automation.tick(broker, explicit=True, strategy=rating_strategy(settings, "Buy"))
        assert result["status"] == ("failed" if attempted else "blocked"), result
        assert bool(result.get("ordinary_pre_submit_skip")) is (not attempted)
        assert automation._state()["failure_count"] == (index + 1 if attempted else 0)
        assert automation.status()["enabled"] is (not attempted or index < 2)
        now += timedelta(seconds=600)
    assert bool(result.get("automatic_paused")) is attempted
    assert len(db.orders(attempted_only=True)) == (3 if attempted else 0)
    broker.submit_order.assert_not_called()


@pytest.mark.parametrize(
    "error_type,outcome", [(BrokerRateLimited, "rate_limited"), (BrokerReadUnavailable, "read_unavailable")]
)
def test_temporary_read_failure_defers_whole_round_and_survives_restart(
    dual_setup, monkeypatch, error_type, outcome
):
    settings, db, broker, automation, strategy = dual_setup
    automation.enable(explicit=True)
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    original = automation_module.reconcile_account
    throttled = Mock(side_effect=error_type(900))
    monkeypatch.setattr(automation_module, "reconcile_account", throttled)
    broker.submit_order = Mock(wraps=broker.submit_order)
    for _ in range(4):
        result = automation.tick(broker, explicit=True, strategy=strategy)
        assert result["status"] == outcome
        assert result["skipped_symbols"] == ["XRP/USD"]
        assert result["results"][0]["retry_after_seconds"] == 900
        assert automation.status()["enabled"]
        assert automation._state()["failure_count"] == 0
        now += timedelta(seconds=901)
    broker.submit_order.assert_not_called()
    strategy.decide.assert_not_called()
    assert throttled.call_count == 4
    last = timestamp(automation._state()["last_started_at"])
    now = last + timedelta(seconds=400)
    restarted = Automation(settings, db)
    restarted.pause()
    restarted.enable(explicit=True)
    assert restarted.tick(broker, explicit=True, strategy=strategy)["status"] == "cooldown"
    assert throttled.call_count == 4
    monkeypatch.setattr(automation_module, "reconcile_account", original)
    now = last + timedelta(seconds=901)
    assert restarted.tick(broker, explicit=True, strategy=strategy)["status"] == "no_order"
    assert strategy.decide.call_count == 2


@pytest.mark.parametrize("error_type", [BrokerRateLimited, BrokerReadUnavailable])
def test_temporary_read_failure_with_unknown_submission_still_halts(setup, monkeypatch, error_type):
    settings, db, broker, automation = setup
    preview = automation_module.run_once(settings, broker, db)
    assert db.claim_submission(preview["order"].client_order_id)
    db.order_state(preview["order"].client_order_id, "unknown")
    automation.enable(explicit=True)
    monkeypatch.setattr(automation_module, "reconcile_account", Mock(side_effect=error_type()))
    broker.submit_order = Mock(wraps=broker.submit_order)
    result = automation.tick(broker, explicit=True)
    assert result["status"] == "halted"
    assert result["reason"] == "unknown_order"
    assert not automation.status()["enabled"]
    broker.submit_order.assert_not_called()


@pytest.mark.parametrize(
    "error_type,outcome", [(BrokerRateLimited, "rate_limited"), (BrokerReadUnavailable, "read_unavailable")]
)
def test_temporary_read_failure_after_confirmed_submission_does_not_repeat_post(
    setup, monkeypatch, error_type, outcome
):
    settings, db, broker, automation = setup
    automation.enable(explicit=True)
    original_execute = automation_module.execute_preview
    broker.submit_order = Mock(wraps=broker.submit_order)

    def execute_then_read_limit(*args, **kwargs):
        original_execute(*args, **kwargs)
        raise error_type(60)

    monkeypatch.setattr(automation_module, "execute_preview", execute_then_read_limit)
    result = automation.tick(broker, explicit=True, strategy=rating_strategy(settings, "Buy"))
    assert result["status"] == outcome
    assert result["retry_after_seconds"] >= automation.interval_seconds
    assert automation.status()["enabled"]
    assert broker.submit_order.call_count == 1
    assert db.orders(attempted_only=True)[0]["status"] == "filled"
    assert automation.tick(broker, explicit=True)["status"] == "cooldown"
    assert broker.submit_order.call_count == 1
