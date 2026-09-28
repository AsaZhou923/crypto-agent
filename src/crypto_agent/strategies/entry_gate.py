"""Durable entry confirmation from consecutive attempts, including failed attempts."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

from crypto_agent.models import AgentError, TradeDecision, dumps, timestamp, utcnow

BULLISH = {"Buy", "Overweight"}
REDUCTIONS = {"Sell", "Underweight"}
RATINGS = BULLISH | REDUCTIONS | {"Hold", "REVIEW"}
FILTER = "confirm2_cooldown30"


def _enabled(settings):
    return settings.strategy.get("intraday_trade_filter") == FILTER


def begin_attempt(database, settings, run_id, symbol, *, cycle_started_at=None, now=None) -> None:
    """Record a failed-until-completed attempt before any network or model request."""
    if not _enabled(settings):
        return
    now = timestamp(now or utcnow())
    cycle = timestamp(cycle_started_at) if cycle_started_at is not None else None
    if cycle is not None and cycle > now:
        raise AgentError("Entry filter cycle timestamp is in the future")
    try:
        with database.connection:
            database.connection.execute(
                """CREATE TABLE IF NOT EXISTS entry_filter_signals (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE, config_digest TEXT NOT NULL,
                    symbol TEXT NOT NULL, cycle_started_at TEXT, started_at TEXT NOT NULL,
                    quote_at TEXT, available_at TEXT, raw_decision TEXT, rating TEXT,
                    valid INTEGER NOT NULL DEFAULT 0 CHECK(valid IN (0, 1)),
                    admitted INTEGER NOT NULL DEFAULT 0 CHECK(admitted IN (0, 1)))"""
            )
            database.connection.execute(
                """INSERT INTO entry_filter_signals
                (run_id,config_digest,symbol,cycle_started_at,started_at) VALUES (?,?,?,?,?)""",
                (run_id, settings.digest, symbol, cycle.isoformat() if cycle else None, now.isoformat()),
            )
    except sqlite3.Error as exc:
        raise AgentError("Cannot persist entry filter attempt") from exc


def _history(row, *, max_age=60, now=None):
    """Validate stored evidence before relying on it; pending rows remain barriers."""
    try:
        started = timestamp(row["started_at"])
        cycle = timestamp(row["cycle_started_at"]) if row["cycle_started_at"] else None
        if cycle and cycle > started:
            raise ValueError
        if row["available_at"] is None:
            if row["valid"] != 0 or any(
                row[key] is not None for key in ("raw_decision", "rating", "quote_at")
            ):
                raise ValueError
            return None
        available = timestamp(row["available_at"])
        quote = timestamp(row["quote_at"])
        raw = json.loads(row["raw_decision"])
        if (
            available < started
            or row["rating"] not in RATINGS
            or raw["rating"] != row["rating"]
            or raw["symbol"] != row["symbol"]
            or row["valid"] not in (0, 1)
            or not isinstance(raw["strategy_version"], str)
            or not isinstance(raw["model"], str)
            or (now is not None and available > now)
        ):
            raise ValueError
        if row["valid"] and (
            raw["evaluation_eligible"] is not True
            or timestamp(raw["created_at"]) > available
            or timestamp(raw["expires_at"]) <= available
            or not 0 <= (available - quote).total_seconds() <= max_age
        ):
            raise ValueError
        return cycle, quote, available, raw
    except (KeyError, TypeError, ValueError, AgentError) as exc:
        raise AgentError("Invalid stored entry filter evidence") from exc


def _suppress(decision, reason, *, valid):
    return replace(
        decision,
        rating="REVIEW" if decision.rating in BULLISH else decision.rating,
        target_position_pct=None,
        actionable=False,
        evaluation_eligible=valid,
        reason=f"{decision.reason} Entry filter: {reason}",
        evidence=(*decision.evidence, f"Entry filter: {reason}"),
    )


def filter_decision(
    database, settings, run_id, decision: TradeDecision, *, market_observed_at, now=None
) -> TradeDecision:
    """Save raw evidence, then suppress entries lacking confirmation or cooldown."""
    if not _enabled(settings):
        return decision
    now = timestamp(now or utcnow())
    quote = timestamp(market_observed_at)
    try:
        current = database.connection.execute(
            "SELECT * FROM entry_filter_signals WHERE run_id=?", (run_id,)
        ).fetchone()
        if (
            current is None
            or current["config_digest"] != settings.digest
            or current["symbol"] != decision.symbol
            or current["available_at"] is not None
            or timestamp(current["started_at"]) > now
            or decision.rating not in RATINGS
        ):
            raise AgentError("Entry filter attempt is missing or inconsistent")
        _history(current)
        valid = (
            decision.evaluation_eligible is True
            and timestamp(decision.created_at) <= now < timestamp(decision.expires_at)
            and 0 <= (now - quote).total_seconds() <= settings.risk["max_data_age_seconds"]
        )
        with database.connection:
            database.connection.execute(
                """UPDATE entry_filter_signals SET quote_at=?,available_at=?,raw_decision=?,rating=?,valid=?
                WHERE run_id=?""",
                (quote.isoformat(), now.isoformat(), dumps(decision), decision.rating, int(valid), run_id),
            )
        if not valid:
            return _suppress(decision, "invalid or expired original evidence; no order", valid=False)
        if decision.rating not in BULLISH:
            return decision
        reason = _entry_barrier(database, settings, current, decision, quote, now)
        if reason:
            return _suppress(decision, reason, valid=True)
        with database.connection:
            database.connection.execute(
                "UPDATE entry_filter_signals SET admitted=1 WHERE run_id=?", (run_id,)
            )
        return decision
    except sqlite3.Error as exc:
        raise AgentError("Cannot read or persist entry filter evidence") from exc


def _entry_barrier(database, settings, current, decision, quote, now):
    previous = database.connection.execute(
        """SELECT * FROM entry_filter_signals WHERE config_digest=? AND symbol=? AND sequence<?
        ORDER BY sequence DESC LIMIT 1""",
        (settings.digest, decision.symbol, current["sequence"]),
    ).fetchone()
    reductions = database.connection.execute(
        """SELECT * FROM entry_filter_signals WHERE config_digest=? AND symbol=? AND sequence<?
        AND valid=1 AND rating IN ('Sell','Underweight')""",
        (settings.digest, decision.symbol, current["sequence"]),
    ).fetchall()
    history_options = {"max_age": settings.risk["max_data_age_seconds"], "now": now}
    reduction_times = [_history(row, **history_options)[2] for row in reductions]
    if reduction_times and now - max(reduction_times) < timedelta(minutes=30):
        return "wait 30 minutes after the latest reduction signal"
    evidence = _history(previous, **history_options) if previous else None
    cycle = timestamp(current["cycle_started_at"]) if current["cycle_started_at"] else None
    confirmed = (
        previous is not None
        and previous["valid"] == 1
        and previous["rating"] in BULLISH
        and evidence is not None
        and cycle is not None
        and evidence[0] is not None
        and (cycle - evidence[0]).total_seconds() >= settings.paper.get("automatic_interval_seconds", 300)
        and 0 < (quote - evidence[1]).total_seconds() <= 600
        and evidence[2] < now
        and evidence[3]["strategy_version"] == decision.strategy_version
        and evidence[3]["model"] == decision.model
    )
    if confirmed and _intervening_cycle(database, evidence[0], cycle):
        return "an intervening scheduled attempt broke consecutive confirmation"
    return None if confirmed else "need two consecutive bullish scheduled observations"


def _intervening_cycle(database, previous, current):
    # Reconciliation can fail before run_once creates a signal row; skipped symbols
    # also have no row. Whole-cycle history closes both confirmation gaps.
    if (
        database.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_cycles'"
        ).fetchone()
        is None
    ):
        return False
    cycles = [timestamp(row[0]) for row in database.connection.execute("SELECT started_at FROM auto_cycles")]
    return current in cycles and any(previous < cycle < current for cycle in cycles)


def validate_entry_preview(database, settings, run_id, decision, *, now=None) -> list[str]:
    """Read-only proof check; never create or repair missing entry authorization."""
    if not _enabled(settings) or decision.rating not in BULLISH:
        return []
    try:
        now = timestamp(now or utcnow())
        current = database.connection.execute(
            "SELECT * FROM entry_filter_signals WHERE run_id=?", (run_id,)
        ).fetchone()
        if (
            current is None
            or current["config_digest"] != settings.digest
            or current["symbol"] != decision.symbol
            or current["valid"] != 1
            or current["admitted"] != 1
            or current["rating"] not in BULLISH
        ):
            return ["Entry filter preview has no matching approved original evidence"]
        evidence = _history(current, max_age=settings.risk["max_data_age_seconds"], now=now)
        if evidence is None or evidence[3] != json.loads(dumps(decision)):
            return ["Entry filter preview does not match original approved decision"]
        if not timestamp(decision.created_at) <= now < timestamp(decision.expires_at):
            return ["Entry filter preview decision is expired or in the future"]
        reason = _entry_barrier(database, settings, current, decision, evidence[1], evidence[2])
        if reason:
            return [f"Entry filter preview proof failed: {reason}"]
        later = database.connection.execute(
            """SELECT * FROM entry_filter_signals WHERE config_digest=? AND symbol=? AND sequence>?
            AND valid=1 AND rating IN ('Sell','Underweight')""",
            (settings.digest, decision.symbol, current["sequence"]),
        ).fetchall()
        for row in later:
            reduction = _history(row, max_age=settings.risk["max_data_age_seconds"], now=now)
            if now - reduction[2] < timedelta(minutes=30):
                return ["Entry filter preview blocked by a later reduction signal cooldown"]
        return []
    except (sqlite3.Error, AgentError, KeyError, TypeError, ValueError):
        return ["Entry filter preview evidence is missing or invalid"]
