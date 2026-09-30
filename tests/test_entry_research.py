"""Offline entry research must not manufacture trades or inspect its holdout."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from crypto_agent import entry_research
from crypto_agent.config import load_settings
from crypto_agent.evaluation import Observation
from crypto_agent.models import AgentError, dumps


def sample(rating="Buy"):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Observation(
        now,
        now + timedelta(seconds=1),
        rating,
        D(50000),
        D(50001),
        D(100000),
        "cohort",
        "intraday-ai-1min-v3-capped-probe",
        "openai:test",
        "paper",
        True,
    )


@pytest.mark.parametrize("rating", ["Sell", "Underweight", "Hold", "REVIEW"])
def test_filter_preserves_all_non_entry_ratings(rating):
    item = sample(rating)
    for variant in entry_research.VARIANTS:
        assert entry_research.filtered_observation(item, None, variant) is item


def test_filters_only_suppress_entries_and_need_original_features():
    item = sample()
    features = SimpleNamespace(
        momentum_score=2,
        return_3m_bps=D(-3),
        return_10m_bps=D(6),
        return_30m_bps=D(6),
        ema_5_vs_20_bps=D(5),
    )
    risk = {"fee_buffer_bps": D(30), "slippage_bps": D(20)}
    assert entry_research.filtered_observation(item, features, "incumbent") is item
    assert entry_research.filtered_observation(item, features, "cooldown30", risk=risk) is item
    for variant in ("entry_score_3", "aligned_short_trend", "cost_cover", "cost_cover_cooldown30"):
        filtered = entry_research.filtered_observation(item, features, variant, risk=risk)
        assert filtered.rating == "REVIEW" and not filtered.actionable
        assert replace(filtered, rating=item.rating, actionable=item.actionable) == item
    for variant in entry_research.VARIANTS[1:]:
        with pytest.raises(AgentError, match="features"):
            entry_research.filtered_observation(item, None, variant)
    features.momentum_score = 3
    features.return_3m_bps = D(2)
    features.return_30m_bps = D(200)
    for variant in entry_research.VARIANTS:
        assert entry_research.filtered_observation(item, features, variant, risk=risk) is item


def test_cooldown_research_variant_is_stateful_without_changing_original_observation():
    item = sample()
    later = replace(item, observed_at=item.observed_at + timedelta(minutes=5))
    later = replace(later, available_at=later.available_at + timedelta(minutes=5))
    features = SimpleNamespace(
        momentum_score=3,
        return_3m_bps=D(200),
        return_10m_bps=D(200),
        return_30m_bps=D(200),
        ema_5_vs_20_bps=D(5),
    )
    risk = {"fee_buffer_bps": D(30), "slippage_bps": D(20)}
    filtered = entry_research.filtered_observation(
        later, features, "cooldown30", risk=risk, last_entry_at=item.available_at
    )
    assert filtered.rating == "REVIEW" and not filtered.actionable
    assert later.rating == "Buy" and later.actionable


def test_research_uses_approved_cohort_and_never_reads_holdout_outcomes(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.sqlite"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE runs(id TEXT, created_at TEXT, config_digest TEXT, config_json TEXT, market_json TEXT);
        CREATE TABLE metadata(key TEXT, value TEXT);
        CREATE TABLE strategy_observations(id INTEGER, run_id TEXT, body TEXT);
        CREATE TABLE decisions(run_id TEXT, body TEXT);
        CREATE TABLE intraday_contexts(run_id TEXT, body TEXT);
    """)
    risk = load_settings(Path("config/demo"), "offline").risk
    strategy = {
        "quick_think_llm": "test",
        "intraday_entry_policy": "capped_probe",
        "intraday_probe_max_position_usd": D(10),
        "intraday_probe_cost_budget_usd": D(".15"),
        "rating_target_pct": {
            "Buy": D(".0005"),
            "Overweight": D(".0003"),
            "Underweight": D(".0001"),
            "Sell": D(0),
        },
    }
    c.execute("INSERT INTO metadata VALUES('automatic_policy',?)", (dumps({"approval_digest": "cohort"}),))
    for index in range(9):
        item = sample("Buy" if index % 2 == 0 else "Sell")
        delta = timedelta(minutes=20 * index)
        item = replace(item, observed_at=item.observed_at + delta, available_at=item.available_at + delta)
        c.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?)",
            (
                str(index),
                item.observed_at.isoformat(),
                "effective-config",
                dumps({"strategy": strategy, "risk": risk}),
                "{}",
            ),
        )
        c.execute("INSERT INTO strategy_observations VALUES(?,?,?)", (index, str(index), dumps(item)))
        c.execute("INSERT INTO decisions VALUES(?,?)", (str(index), "{}"))
        c.execute("INSERT INTO intraday_contexts VALUES(?,?)", (str(index), "[]"))
    c.commit()
    assets = tmp_path / "assets.json"
    assets.write_text(
        dumps(
            {
                "assets": {
                    s: {
                        "symbol": s,
                        "tradable": True,
                        "min_order_size": "0.000000001",
                        "quantity_increment": "0.000000001",
                        "price_increment": "0.000000001",
                    }
                    for s in ("BTC/USD", "XRP/USD")
                }
            }
        )
    )
    inspected = []

    def features(row, strategy):
        inspected.append(row["id"])
        return SimpleNamespace(
            momentum_score=2,
            return_3m_bps=D(-3),
            return_10m_bps=D(6),
            return_30m_bps=D(6),
            ema_5_vs_20_bps=D(5),
        )

    monkeypatch.setattr(entry_research, "_features", features)
    first = entry_research.compare(path, assets)
    assert first["replay_min_interval_seconds"] == 600
    assert first["replay_max_gap_seconds"] == 600
    assert first["symbols"]["BTC/USD"]["total_observations"] == 9
    assert first["symbols"]["BTC/USD"]["training_observations"] == 6
    assert first["holdout_evaluated"] is False
    assert first["deployment_recommended"] is False
    assert max(inspected) < 6
    for index in range(6, 9):
        raw = json.loads(
            c.execute("SELECT body FROM strategy_observations WHERE id=?", (index,)).fetchone()[0]
        )
        raw.update(bid="-1", ask="NaN", rating="INVALID")
        c.execute("UPDATE strategy_observations SET body=? WHERE id=?", (dumps(raw), index))
    c.commit()
    second = entry_research.compare(path, assets)
    assert second == first
    unrelated = {"strategy": {**strategy, "quick_think_llm": "unapproved-model"}, "risk": risk}
    c.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?)",
        ("unrelated", "2030-01-01T00:00:00+00:00", "unapproved", dumps(unrelated), "{}"),
    )
    c.commit()
    assert entry_research.compare(path, assets) == first
    changed = {"strategy": {**strategy, "intraday_probe_max_position_usd": D(100)}, "risk": risk}
    c.execute("UPDATE runs SET config_json=? WHERE id='0'", (dumps(changed),))
    c.commit()
    with pytest.raises(AgentError, match="mix effective run configurations"):
        entry_research.compare(path, assets)
    c.close()


def test_research_derives_current_paper_rotation_cadence_and_segments_gaps(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.sqlite"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE runs(id TEXT, created_at TEXT, config_digest TEXT, config_json TEXT, market_json TEXT);
        CREATE TABLE metadata(key TEXT, value TEXT);
        CREATE TABLE strategy_observations(id INTEGER, run_id TEXT, body TEXT);
        CREATE TABLE decisions(run_id TEXT, body TEXT);
        CREATE TABLE intraday_contexts(run_id TEXT, body TEXT);
    """)
    risk = load_settings(Path("config/demo"), "offline").risk
    strategy = {
        "quick_think_llm": "test",
        "intraday_entry_policy": "capped_probe",
        "intraday_probe_max_position_usd": D(10),
        "intraday_probe_cost_budget_usd": D(".15"),
        "rating_target_pct": {
            "Buy": D(".0005"),
            "Overweight": D(".0003"),
            "Underweight": D(".0001"),
            "Sell": D(0),
        },
    }
    config = {
        "paper": {"automatic_interval_seconds": 300, "automatic_symbols_per_cycle": 2},
        "strategy": strategy,
        "risk": risk,
    }
    c.execute("INSERT INTO metadata VALUES('automatic_policy',?)", (dumps({"approval_digest": "cohort"}),))
    offsets = [0, 300, 901, 1201, 1501, 1801]
    for index, seconds in enumerate(offsets):
        item = sample("Buy")
        delta = timedelta(seconds=seconds)
        item = replace(item, observed_at=item.observed_at + delta, available_at=item.available_at + delta)
        c.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?)",
            (str(index), item.observed_at.isoformat(), "effective-config", dumps(config), "{}"),
        )
        c.execute("INSERT INTO strategy_observations VALUES(?,?,?)", (index, str(index), dumps(item)))
        c.execute("INSERT INTO decisions VALUES(?,?)", (str(index), "{}"))
        c.execute("INSERT INTO intraday_contexts VALUES(?,?)", (str(index), "[]"))
    c.commit()
    assets = tmp_path / "assets.json"
    assets.write_text(
        dumps(
            {
                "assets": {
                    s: {
                        "symbol": s,
                        "tradable": True,
                        "min_order_size": "0.000000001",
                        "quantity_increment": "0.000000001",
                        "price_increment": "0.000000001",
                    }
                    for s in ("BTC/USD", "XRP/USD")
                }
            }
        )
    )
    inspected = []

    def features(row, strategy):
        inspected.append(row["id"])
        return SimpleNamespace(
            momentum_score=3,
            return_3m_bps=D(200),
            return_10m_bps=D(200),
            return_30m_bps=D(200),
            ema_5_vs_20_bps=D(5),
        )

    monkeypatch.setattr(entry_research, "_features", features)
    report = entry_research.compare(path, assets)
    assert report["replay_min_interval_seconds"] == 300
    assert report["replay_max_gap_seconds"] == 600
    assert report["symbols"]["BTC/USD"]["training_observations"] == 4
    assert report["symbols"]["BTC/USD"]["withheld_observations"] == 2
    assert report["symbols"]["BTC/USD"]["segment_count"] == 2
    assert max(inspected) < 4
    c.close()
