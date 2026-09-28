"""Exercise v5 wiring with a local synthetic broker and deterministic decisions."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from crypto_agent.automation import Automation
from crypto_agent.brokers.offline import OfflineBroker
from crypto_agent.config import load_settings
from crypto_agent.models import BrokerReadUnavailable, PriceBar, TradeDecision
from crypto_agent.runner import execute_preview, run_once
from crypto_agent.storage.database import Database


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 24, 0, 0, tzinfo=UTC)]
    for module in (
        "crypto_agent.automation",
        "crypto_agent.runner",
        "crypto_agent.strategies.entry_gate",
        "crypto_agent.brokers.offline",
        "crypto_agent.risk.checks",
        "crypto_agent.storage.database",
    ):
        monkeypatch.setattr(f"{module}.utcnow", lambda: clock[0])
    # Construct synthetic Settings directly: production load_settings rejects
    # this Paper experiment in offline mode, but no live adapter is used here.
    base = load_settings(Path("config/demo"), mode="offline")
    settings = replace(
        base,
        paper={
            **base.paper,
            "database_path": str(tmp_path / "ledger.sqlite"),
            "trigger": "scheduled",
            "trading_enabled": True,
            "automatic_interval_seconds": 300,
        },
        strategy={
            **base.strategy,
            "name": "intraday_ai",
            "intraday_probe_profile": "expanded_paper",
            "intraday_entry_policy": "capped_probe",
            "intraday_trade_filter": "confirm2_cooldown30",
            "intraday_probe_max_position_usd": Decimal(5000),
            "intraday_probe_cost_budget_usd": Decimal(75),
            "rating_target_pct": {
                "Buy": Decimal(".05"),
                "Overweight": Decimal(".03"),
                "Underweight": Decimal(".01"),
                "Sell": Decimal(0),
            },
        },
    )
    db = Database(settings.database_path, "offline")
    broker = OfflineBroker(tmp_path / "broker.sqlite")
    broker.submit_order = Mock(wraps=broker.submit_order)

    def decide(market, portfolio):
        return TradeDecision(
            symbol=market.symbol,
            target_position_pct=Decimal(".01"),
            reason="Synthetic bullish evidence",
            expires_at=clock[0] + timedelta(seconds=120),
            created_at=clock[0],
            rating="Buy",
            strategy_version="intraday-ai-1min-v5-low-turnover-paper",
            model="synthetic-test-model",
            evidence=("Deterministic integration fixture; no external model",),
        )

    strategy = Mock(decide=Mock(side_effect=decide))
    yield settings, db, broker, strategy, clock
    broker.close()
    db.close()


def scheduled_run(setup):
    settings, db, broker, strategy, clock = setup
    return run_once(settings, broker, db, strategy=strategy, cycle_started_at=clock[0])


def confirmed_preview(setup):
    first = scheduled_run(setup)
    assert first["status"] == "no_order"
    setup[4][0] += timedelta(seconds=300)
    second = scheduled_run(setup)
    assert second["status"] == "preview", second
    return first, second


def test_runner_persists_raw_predecessor_before_confirmed_preview(setup):
    _, db, broker, _, _ = setup
    first, second = confirmed_preview(setup)
    assert first["decision"].rating == "REVIEW"
    assert second["decision"].rating == "Buy"
    rows = db.connection.execute("SELECT * FROM entry_filter_signals ORDER BY sequence").fetchall()
    assert [row["run_id"] for row in rows] == [first["run_id"], second["run_id"]]
    assert all(row["valid"] == 1 and row["rating"] == "Buy" for row in rows)
    assert json.loads(rows[0]["raw_decision"])["rating"] == "Buy"
    assert db.get_run(first["run_id"])["decision"].rating == "REVIEW"
    broker.submit_order.assert_not_called()


def test_identity_read_failure_is_pending_barrier_to_next_confirmation(setup, monkeypatch):
    settings, db, broker, strategy, clock = setup
    assert scheduled_run(setup)["status"] == "no_order"
    clock[0] += timedelta(seconds=300)
    original = broker.get_portfolio
    monkeypatch.setattr(broker, "get_portfolio", Mock(side_effect=BrokerReadUnavailable()))
    with pytest.raises(BrokerReadUnavailable):
        scheduled_run(setup)
    pending = db.connection.execute(
        "SELECT * FROM entry_filter_signals ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    assert pending["valid"] == 0 and pending["available_at"] is None
    assert pending["raw_decision"] is None
    assert db.get_run(pending["run_id"])["status"] == "failed"
    monkeypatch.setattr(broker, "get_portfolio", original)
    clock[0] += timedelta(seconds=300)
    result = scheduled_run(setup)
    assert result["status"] == "no_order" and result["decision"].rating == "REVIEW"
    assert strategy.decide.call_count == 2
    assert not db.orders()
    broker.submit_order.assert_not_called()


def test_manual_runs_cannot_accumulate_scheduled_entry_confirmation(setup):
    settings, db, broker, strategy, clock = setup
    for _ in range(3):
        result = run_once(settings, broker, db, strategy=strategy)
        assert result["status"] == "no_order" and result["decision"].rating == "REVIEW"
        clock[0] += timedelta(seconds=300)
    assert scheduled_run(setup)["status"] == "no_order"
    assert not db.orders()
    broker.submit_order.assert_not_called()


def test_model_completion_with_expired_closed_bars_is_not_valid_entry_evidence(setup, monkeypatch):
    settings, db, broker, strategy, clock = setup
    settings = replace(settings, strategy={**settings.strategy, "intraday_max_bar_age_seconds": 180})
    started = clock[0]
    last_end = started - timedelta(seconds=179)
    bars = tuple(
        PriceBar(
            "BTC/USD",
            last_end - timedelta(minutes=60 - index),
            Decimal(50000),
            Decimal(50000),
            Decimal(50000),
            Decimal(50000),
            Decimal(1),
            "OFFLINE SYNTHETIC TEST DATA",
        )
        for index in range(60)
    )
    monkeypatch.setattr(broker, "get_bars", Mock(return_value=bars))
    original_decide = strategy.decide

    def delayed_decide(market, portfolio, supplied_bars):
        decision = original_decide(market, portfolio)
        assert supplied_bars == bars
        clock[0] += timedelta(seconds=2)
        return decision

    slow = Mock(requires_intraday_bars=True, bar_request={}, decide=Mock(side_effect=delayed_decide))
    result = run_once(settings, broker, db, strategy=slow, cycle_started_at=started)
    assert result["status"] == "no_order", result
    assert result["decision"].rating == "REVIEW"
    assert result["decision"].evaluation_eligible is False
    row = db.connection.execute(
        "SELECT * FROM entry_filter_signals WHERE run_id=?", (result["run_id"],)
    ).fetchone()
    assert row["valid"] == 0
    assert (clock[0] - started).total_seconds() == 2  # Quote still meets its 60-second deadline.
    broker.submit_order.assert_not_called()


def test_automation_supplies_distinct_cycle_identity_and_only_then_executes(setup):
    settings, db, broker, strategy, clock = setup
    auto = Automation(settings, db)
    auto.enable(explicit=True)
    first = auto.tick(broker, explicit=True, strategy=strategy)
    assert first["status"] == "no_order", first
    broker.submit_order.assert_not_called()
    assert auto.tick(broker, explicit=True, strategy=strategy)["status"] == "cooldown"
    clock[0] += timedelta(seconds=300)
    second = auto.tick(broker, explicit=True, strategy=strategy)
    assert second["status"] == "filled", second
    assert broker.submit_order.call_count == 1
    signals = db.connection.execute(
        "SELECT cycle_started_at FROM entry_filter_signals ORDER BY sequence"
    ).fetchall()
    cycles = db.connection.execute("SELECT started_at FROM auto_cycles ORDER BY started_at").fetchall()
    assert [row[0] for row in signals] == [row[0] for row in cycles]
    assert len(signals) == 2 and signals[0][0] != signals[1][0]


@pytest.mark.parametrize("tamper", ["missing_current", "missing_previous", "raw_previous", "current_cycle"])
def test_execute_preview_rejects_missing_or_tampered_confirmation_before_post(setup, tamper):
    settings, db, broker, _, _ = setup
    first, second = confirmed_preview(setup)
    with db.connection:
        if tamper.startswith("missing"):
            victim = second if tamper == "missing_current" else first
            db.connection.execute("DELETE FROM entry_filter_signals WHERE run_id=?", (victim["run_id"],))
        elif tamper == "raw_previous":
            db.connection.execute(
                "UPDATE entry_filter_signals SET raw_decision='{}' WHERE run_id=?", (first["run_id"],)
            )
        else:
            db.connection.execute(
                "UPDATE entry_filter_signals SET cycle_started_at=NULL WHERE run_id=?", (second["run_id"],)
            )
    result = execute_preview(settings, broker, db, second["run_id"], explicit=True)
    assert result["status"] == "blocked", result
    assert result["risk"].reasons
    assert db.order_for_run(second["run_id"])["attempted"] == 0
    broker.submit_order.assert_not_called()
