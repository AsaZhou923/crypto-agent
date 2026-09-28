"""Synthetic, local-only tests of durable anti-churn evidence."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_agent.config import load_settings
from crypto_agent.models import AgentError, TradeDecision
from crypto_agent.storage.database import Database
from crypto_agent.strategies.entry_gate import begin_attempt, filter_decision

START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def setup(tmp_path):
    settings = load_settings(Path("config/demo"), mode="offline")
    settings = replace(
        settings,
        paper={**settings.paper, "automatic_interval_seconds": 300},
        strategy={**settings.strategy, "intraday_trade_filter": "confirm2_cooldown30"},
    )
    db = Database(tmp_path / "gate.sqlite", "offline")
    yield settings, db
    db.close()


def decision(at, rating="Buy", **kwargs):
    return replace(
        TradeDecision(
            "BTC/USD",
            Decimal("0.05"),
            "original reason",
            at + timedelta(seconds=240),
            at,
            rating,
            "v5",
            "test",
            ("raw signal",),
        ),
        **kwargs,
    )


def observe(setup, seconds, rating="Buy", *, run_id=None, cycle=True, quote_age=0, **kwargs):
    settings, db = setup
    at = START + timedelta(seconds=seconds)
    run_id = run_id or str(seconds)
    begin_attempt(db, settings, run_id, "BTC/USD", cycle_started_at=at if cycle else None, now=at)
    original = decision(at, rating, **kwargs)
    result = filter_decision(
        db, settings, run_id, original, market_observed_at=at - timedelta(seconds=quote_age), now=at
    )
    return result, original


def test_first_suppressed_raw_saved_and_second_confirmed(setup):
    first, original = observe(setup, 0)
    assert first.rating == "REVIEW" and not first.actionable and first.evaluation_eligible
    assert first.target_position_pct is None
    row = setup[1].connection.execute("SELECT * FROM entry_filter_signals").fetchone()
    assert row["rating"] == "Buy" and row["valid"] == 1
    assert '"rating": "Buy"' in row["raw_decision"]
    second, raw = observe(setup, 300, "Overweight")
    assert second == raw


@pytest.mark.parametrize("rating", ["Sell", "Underweight", "Hold", "REVIEW"])
def test_nonbullish_unchanged_and_resets_confirmation(setup, rating):
    observe(setup, 0)
    result, raw = observe(setup, 300, rating)
    assert result == raw
    assert observe(setup, 600)[0].rating == "REVIEW"


@pytest.mark.parametrize(
    "failure", ["pending", "ineligible", "stale", "future_quote", "expired", "future_decision"]
)
def test_failed_attempt_breaks_confirmation(setup, failure):
    observe(setup, 0)
    if failure == "pending":
        begin_attempt(
            setup[1],
            setup[0],
            "failure",
            "BTC/USD",
            cycle_started_at=START + timedelta(seconds=300),
            now=START + timedelta(seconds=300),
        )
    else:
        kwargs = {
            "ineligible": {"evaluation_eligible": False},
            "stale": {"quote_age": 61},
            "future_quote": {"quote_age": -1},
            "expired": {"expires_at": START + timedelta(seconds=300)},
            "future_decision": {"created_at": START + timedelta(seconds=301)},
        }[failure]
        failed, _ = observe(setup, 300, **kwargs)
        assert not failed.evaluation_eligible and not failed.actionable
    assert observe(setup, 600)[0].rating == "REVIEW"


@pytest.mark.parametrize("gap,allowed", [(299, False), (300, True), (600, True), (601, False)])
def test_cycle_interval_and_quote_gap_edges(setup, gap, allowed):
    observe(setup, 0)
    result, raw = observe(setup, gap)
    assert (result == raw) == allowed


@pytest.mark.parametrize("first_manual", [True, False])
def test_manual_attempt_never_confirms(setup, first_manual):
    observe(setup, 0, cycle=not first_manual)
    assert observe(setup, 300, cycle=first_manual)[0].rating == "REVIEW"


def test_same_cycle_never_confirms_even_with_later_quote(setup):
    settings, db = setup
    observe(setup, 0)
    at = START + timedelta(seconds=300)
    begin_attempt(db, settings, "second", "BTC/USD", cycle_started_at=START, now=at)
    result = filter_decision(db, settings, "second", decision(at), market_observed_at=at, now=at)
    assert result.rating == "REVIEW"


@pytest.mark.parametrize("elapsed,allowed", [(1799, False), (1800, True)])
def test_reduction_cooldown_30_minute_edge(setup, elapsed, allowed):
    observe(setup, 0, "Sell")
    observe(setup, elapsed - 300)
    result, raw = observe(setup, elapsed)
    assert (result == raw) == allowed


def test_latest_reduction_restarts_cooldown(setup):
    observe(setup, 0, "Sell")
    observe(setup, 1200, "Underweight")
    observe(setup, 1500)
    assert observe(setup, 1800)[0].rating == "REVIEW"


def test_restart_retains_confirmation(setup):
    settings, db = setup
    observe(setup, 0)
    reopened = Database(db.path, "offline")
    try:
        result, raw = observe((settings, reopened), 300)
        assert result == raw
    finally:
        reopened.close()


@pytest.mark.parametrize("change", ["config", "version", "model"])
def test_evidence_does_not_cross_configuration_or_strategy(setup, change):
    settings, db = setup
    observe(setup, 0)
    if change == "config":
        settings = replace(settings, strategy={**settings.strategy, "version": "changed"})
    kwargs = (
        {"strategy_version": "v6"}
        if change == "version"
        else {"model": "changed"}
        if change == "model"
        else {}
    )
    assert observe((settings, db), 300, **kwargs)[0].rating == "REVIEW"


def test_other_symbol_cannot_confirm(setup):
    settings, db = setup
    begin_attempt(db, settings, "xrp", "XRP/USD", cycle_started_at=START, now=START)
    filter_decision(
        db, settings, "xrp", decision(START, symbol="XRP/USD"), market_observed_at=START, now=START
    )
    assert observe(setup, 300)[0].rating == "REVIEW"


def test_invalid_reduction_does_not_establish_cooldown(setup):
    observe(setup, 0, "Sell", evaluation_eligible=False)
    observe(setup, 300)
    result, raw = observe(setup, 600)
    assert result == raw


def test_disabled_is_noop_without_table(setup):
    settings, db = setup
    settings = replace(settings, strategy={**settings.strategy, "intraday_trade_filter": None})
    begin_attempt(db, settings, "none", "BTC/USD")
    raw = decision(START)
    assert filter_decision(db, settings, "none", raw, market_observed_at=START) == raw
    assert (
        db.connection.execute("SELECT name FROM sqlite_master WHERE name='entry_filter_signals'").fetchone()
        is None
    )


def test_missing_duplicate_and_corrupt_attempts_fail_closed(setup):
    settings, db = setup
    observe(setup, 0)
    with pytest.raises(AgentError):
        begin_attempt(db, settings, "0", "BTC/USD", now=START)
    with pytest.raises(AgentError):
        filter_decision(db, settings, "missing", decision(START), market_observed_at=START, now=START)
    with db.connection:
        db.connection.execute("UPDATE entry_filter_signals SET raw_decision='not json'")
    with pytest.raises(AgentError, match="stored"):
        observe(setup, 300)


def test_future_cycle_rejected(setup):
    with pytest.raises(AgentError, match="future"):
        begin_attempt(
            setup[1], setup[0], "bad", "BTC/USD", cycle_started_at=START + timedelta(seconds=1), now=START
        )


def test_presubmit_proof_is_read_only_and_rejects_later_reduction(setup):
    from crypto_agent.strategies.entry_gate import validate_entry_preview

    settings, db = setup
    observe(setup, 0)
    approved, raw = observe(setup, 300)
    changes = db.connection.total_changes
    assert validate_entry_preview(db, settings, "300", approved, now=START + timedelta(seconds=301)) == []
    assert db.connection.total_changes == changes
    observe(setup, 400, "Underweight")
    assert (
        "later reduction"
        in validate_entry_preview(db, settings, "300", raw, now=START + timedelta(seconds=401))[0]
    )


@pytest.mark.parametrize("case", ["first_suppressed", "missing", "changed", "expired", "future", "config"])
def test_presubmit_rejects_missing_or_changed_proof(setup, case):
    from crypto_agent.strategies.entry_gate import validate_entry_preview

    settings, db = setup
    _, first = observe(setup, 0)
    approved, _ = observe(setup, 300)
    run_id, at = "300", START + timedelta(seconds=301)
    if case == "first_suppressed":
        approved, run_id, at = first, "0", START + timedelta(seconds=1)
    elif case == "missing":
        run_id = "missing"
    elif case == "changed":
        approved = replace(approved, target_position_pct=Decimal("0.04"))
    elif case == "expired":
        at = START + timedelta(seconds=540)
    elif case == "future":
        at = START + timedelta(seconds=299)
    elif case == "config":
        settings = replace(settings, strategy={**settings.strategy, "version": "changed"})
    assert validate_entry_preview(db, settings, run_id, approved, now=at)


def test_presubmit_missing_table_fails_closed_but_reductions_pass(setup):
    from crypto_agent.strategies.entry_gate import validate_entry_preview

    settings, db = setup
    assert validate_entry_preview(db, settings, "missing", decision(START), now=START)
    assert validate_entry_preview(db, settings, "missing", decision(START, "Sell"), now=START) == []


@pytest.mark.parametrize(
    "column,value", [("available_at", (START + timedelta(seconds=301)).isoformat()), ("raw_decision", "{}")]
)
def test_future_or_malformed_history_fails_closed(setup, column, value):
    observe(setup, 0)
    with setup[1].connection:
        setup[1].connection.execute(f"UPDATE entry_filter_signals SET {column}=?", (value,))
    with pytest.raises(AgentError, match="stored"):
        observe(setup, 300)


def test_equal_availability_cannot_confirm(setup):
    settings, db = setup
    begin_attempt(db, settings, "first", "BTC/USD", cycle_started_at=START, now=START)
    at = START + timedelta(seconds=300)
    filter_decision(db, settings, "first", decision(at), market_observed_at=at, now=at)
    assert observe(setup, 300)[0].rating == "REVIEW"


@pytest.mark.parametrize("status", ["failed", "blocked", "rate_limited", "pending", "filled"])
def test_intervening_whole_cycle_breaks_confirmation_without_symbol_attempt(setup, status):
    settings, db = setup
    with db.connection:
        db.connection.execute("CREATE TABLE auto_cycles (id TEXT PRIMARY KEY, started_at TEXT, status TEXT)")
        db.connection.executemany(
            "INSERT INTO auto_cycles VALUES (?,?,?)",
            [
                ("first", START.isoformat(), "no_order"),
                ("skipped-symbol", (START + timedelta(seconds=300)).isoformat(), status),
                ("current", (START + timedelta(seconds=600)).isoformat(), "running"),
            ],
        )
    observe(setup, 0)
    result, _ = observe(setup, 600)
    assert result.rating == "REVIEW" and "intervening" in result.reason


def test_adjacent_recorded_cycles_allow_confirmation(setup):
    settings, db = setup
    with db.connection:
        db.connection.execute("CREATE TABLE auto_cycles (started_at TEXT)")
        db.connection.executemany(
            "INSERT INTO auto_cycles VALUES (?)",
            [(START.isoformat(),), ((START + timedelta(seconds=300)).isoformat(),)],
        )
    observe(setup, 0)
    result, raw = observe(setup, 300)
    assert result == raw


def test_same_quote_cannot_confirm_across_slow_adjacent_cycles(setup):
    settings, db = setup
    begin_attempt(db, settings, "slow", "BTC/USD", cycle_started_at=START, now=START)
    quote = START + timedelta(seconds=300)
    filter_decision(db, settings, "slow", decision(quote), market_observed_at=quote, now=quote)
    at = quote + timedelta(seconds=1)
    begin_attempt(db, settings, "next", "BTC/USD", cycle_started_at=quote, now=at)
    result = filter_decision(db, settings, "next", decision(at), market_observed_at=quote, now=at)
    assert result.rating == "REVIEW"
