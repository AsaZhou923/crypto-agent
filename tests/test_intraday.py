"""Minute-feature and fast AI adapter tests; no external model or broker calls."""

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

import crypto_agent.strategies.intraday_ai as adapter
from crypto_agent.brokers.offline import OfflineBroker
from crypto_agent.config import load_settings
from crypto_agent.data.features import build_intraday_features
from crypto_agent.models import (
    AgentError,
    BrokerOrder,
    MarketSnapshot,
    PortfolioSnapshot,
    Position,
    PriceBar,
)
from crypto_agent.runner import run_once
from crypto_agent.storage.database import Database
from crypto_agent.strategies.baseline import BaselineStrategy
from crypto_agent.strategies.intraday_ai import IntradayAIStrategy

NOW = datetime(2026, 9, 19, 6, 0, tzinfo=UTC)


def bars(*, slope=D("0.01"), end=NOW, count=61):
    result = []
    start = end.replace(second=0, microsecond=0) - timedelta(minutes=count)
    for index in range(count):
        close = D(100) + slope * index
        result.append(
            PriceBar(
                "BTC/USD",
                start + timedelta(minutes=index),
                close - D("0.01"),
                close + D("0.02"),
                close - D("0.02"),
                close,
                D(10 + index),
                "test-1min-bars",
            )
        )
    return tuple(result)


@pytest.fixture
def market():
    return MarketSnapshot("BTC/USD", D("100.60"), NOW, D("100.59"), D("100.61"), "test-book")


@pytest.fixture
def portfolio():
    return PortfolioSnapshot(D(10000), D(10000), (), NOW, D(10000), account_id="paper-test")


@pytest.fixture
def config():
    return {
        "asset_type": "crypto",
        "decision_profile": "intraday_10m",
        "llm_provider": "openai",
        "quick_think_llm": "test-model",
        "backend_url": "https://llm.example/v1",
        "timeout_seconds": 2,
        "decision_ttl_seconds": 120,
        "intraday_lookback_bars": 60,
        "intraday_min_bars": 45,
        "intraday_max_bar_age_seconds": 180,
        "intraday_momentum_threshold_bps": D(2),
        "intraday_entry_score": 2,
        "intraday_entry_policy": "cost_cover",
        "rating_target_pct": {
            "Buy": D("0.0005"),
            "Overweight": D("0.0003"),
            "Underweight": D("0.0001"),
            "Sell": D(0),
        },
    }


@pytest.fixture
def risk():
    return {"fee_buffer_bps": D(1), "slippage_bps": D(1)}


def output(rating="Overweight"):
    return {
        "rating": rating,
        "summary": "短周期动量与均线方向一致。",
        "evidence": ["3分钟收益为正", "EMA5高于EMA20"],
    }


def test_features_use_only_closed_fresh_bars_and_compute_momentum(market):
    features = build_intraday_features(
        market,
        bars(),
        lookback_bars=60,
        min_bars=45,
        max_age_seconds=180,
        momentum_threshold_bps=D(2),
        now=NOW,
    )
    assert features.bar_count == 60
    assert features.last_bar_at == NOW - timedelta(minutes=1)
    assert features.return_3m_bps > 0
    assert features.momentum_score == 4
    assert features.recent_volume_ratio > 1


@pytest.mark.parametrize(
    "invalid,match",
    [
        (bars(count=20), "Too few"),
        (bars(end=NOW - timedelta(minutes=10)), "stale"),
        (bars()[:-2] + (replace(bars()[-1], observed_at=NOW + timedelta(minutes=2)),), "future"),
    ],
)
def test_missing_stale_and_future_bars_fail_closed(market, invalid, match):
    with pytest.raises(AgentError, match=match):
        build_intraday_features(
            market,
            invalid,
            lookback_bars=60,
            min_bars=45,
            max_age_seconds=180,
            momentum_threshold_bps=D(2),
            now=NOW,
        )


def test_strict_output_maps_rating_to_small_target(config, risk, market, portfolio):
    strategy = IntradayAIStrategy(config, risk)
    features = strategy._features(market, bars(), now=NOW)
    decision = strategy.parse_output(output(), market, portfolio, features, NOW, now=NOW)
    assert decision.actionable
    assert decision.rating == "Overweight"
    assert decision.target_position_pct == D("0.0003")
    assert decision.strategy_version == "intraday-ai-1min-v3.1-capped-probe"


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        {"rating": "BUY", "summary": "x", "evidence": ["a", "b"]},
        {"rating": "Buy", "summary": "x", "evidence": ["only one"]},
        {"rating": "Buy", "summary": "x", "evidence": ["a", "b"], "size": "100%"},
    ],
)
def test_malformed_model_output_is_review(config, risk, market, portfolio, invalid):
    strategy = IntradayAIStrategy(config, risk)
    features = strategy._features(market, bars(), now=NOW)
    decision = strategy.parse_output(invalid, market, portfolio, features, NOW, now=NOW)
    assert decision.rating == "REVIEW"
    assert not decision.actionable
    assert decision.target_position_pct is None


def test_ai_cannot_open_long_without_quantitative_confirmation(config, risk, market, portfolio):
    strategy = IntradayAIStrategy(config, risk)
    flat = bars(slope=D(0))
    features = strategy._features(replace(market, price=D(100)), flat, now=NOW)
    decision = strategy.parse_output(output("Buy"), market, portfolio, features, NOW, now=NOW)
    assert decision.rating == "REVIEW"
    assert not decision.actionable


def test_entry_is_review_when_move_proxy_does_not_cover_round_trip_costs(config, market, portfolio):
    strategy = IntradayAIStrategy(config, {"fee_buffer_bps": D(30), "slippage_bps": D(20)})
    features = strategy._features(market, bars(), now=NOW)
    assert features.momentum_score == 4
    decision = strategy.parse_output(output("Buy"), market, portfolio, features, NOW, now=NOW)
    assert decision.rating == "REVIEW"
    assert not decision.actionable
    assert "round-trip cost" in decision.evidence[0]


@pytest.mark.parametrize(
    "rating,score,actionable",
    [
        ("Underweight", -1, False),
        ("Underweight", -2, True),
        ("Sell", -2, False),
        ("Sell", -3, True),
    ],
)
def test_reductions_require_symmetric_negative_momentum(
    config, risk, market, portfolio, rating, score, actionable
):
    strategy = IntradayAIStrategy(config, risk)
    features = replace(strategy._features(market, bars(), now=NOW), momentum_score=score)
    held = replace(
        portfolio,
        cash_usd=D(9990),
        positions=(Position("BTC/USD", D("0.05"), D(100), D("0.05")),),
    )
    decision = strategy.parse_output(output(rating), market, held, features, NOW, now=NOW)
    assert decision.actionable is actionable
    assert decision.rating == (rating if actionable else "REVIEW")


def test_review_and_hold_never_create_an_actionable_order(config, risk, market, portfolio):
    strategy = IntradayAIStrategy(config, risk)
    features = strategy._features(market, bars(), now=NOW)
    review = strategy.parse_output(output("REVIEW"), market, portfolio, features, NOW, now=NOW)
    hold = strategy.parse_output(output("Hold"), market, portfolio, features, NOW, now=NOW)
    assert not review.actionable and review.target_position_pct is None
    assert not hold.actionable and hold.target_position_pct == 0


def test_worker_receives_features_and_model_key_but_no_broker_credentials(
    config, risk, market, portfolio, monkeypatch
):
    strategy = IntradayAIStrategy(config, risk)
    monkeypatch.setattr(adapter, "utcnow", lambda: NOW)
    monkeypatch.setenv("OPENAI_API_KEY", "local-model-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "never-forward-broker-secret")

    def run(*args, **kwargs):
        payload = json.loads(kwargs["input"])
        assert payload["context"]["features"]["return_3m_bps"]
        assert "local-model-key" not in kwargs["input"]
        assert "never-forward-broker-secret" not in kwargs["input"]
        assert kwargs["env"]["OPENAI_API_KEY"] == "local-model-key"
        assert "ALPACA_SECRET_KEY" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(output()))

    monkeypatch.setattr(adapter.subprocess, "run", run)
    decision = strategy.decide(market, portfolio, bars())
    assert decision.rating == "Overweight" and decision.actionable


def test_timeout_is_review_and_never_leaks_worker_output(config, risk, market, portfolio, monkeypatch):
    strategy = IntradayAIStrategy(config, risk)
    monkeypatch.setattr(adapter, "utcnow", lambda: NOW)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("worker", 2, output="provider-secret")

    monkeypatch.setattr(adapter.subprocess, "run", timeout)
    decision = strategy.decide(market, portfolio, bars())
    assert decision.rating == "REVIEW" and not decision.actionable
    assert "secret" not in decision.reason


def test_open_orders_block_model_call(config, risk, market, portfolio, monkeypatch):
    pending = BrokerOrder("id", "client", "BTC/USD", "buy", D(1), D(0), None, "new", NOW)
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: pytest.fail("no model call"))
    decision = IntradayAIStrategy(config, risk).decide(
        market, replace(portfolio, open_orders=(pending,)), bars()
    )
    assert decision.rating == "REVIEW" and not decision.actionable


def test_runner_fetches_and_persists_intraday_context_before_decision(tmp_path):
    settings = load_settings(Path("config/demo"), mode="offline")
    settings = replace(settings, paper={**settings.paper, "database_path": str(tmp_path / "ledger.sqlite")})
    broker = OfflineBroker(tmp_path / "broker.sqlite")
    database = Database(settings.database_path, "offline")
    baseline = BaselineStrategy(settings.strategy)
    captured = {}

    class IntradayFixture:
        requires_intraday_bars = True
        bar_request = {"timeframe": "1Min", "limit": 60}

        def decide(self, selected_market, selected_portfolio, selected_bars):
            captured["bars"] = selected_bars
            return baseline.decide(selected_market, selected_portfolio)

    try:
        result = run_once(settings, broker, database, strategy=IntradayFixture())
        stored = database.connection.execute(
            "SELECT body FROM intraday_contexts WHERE run_id=?", (result["run_id"],)
        ).fetchone()
        assert result["status"] == "preview"
        assert len(captured["bars"]) == 60
        assert stored is not None and "OFFLINE SYNTHETIC TEST DATA" in stored["body"]
    finally:
        broker.close()
        database.close()


def probe_config(config, **changes):
    return {
        **config,
        "name": "intraday_ai",
        "intraday_entry_policy": "capped_probe",
        "intraday_probe_max_position_usd": D(10),
        "intraday_probe_cost_budget_usd": D(".15"),
        **changes,
    }


@pytest.mark.parametrize(
    "trade_filter,version",
    [
        (None, "intraday-ai-1min-v4.1-expanded-paper"),
        ("none", "intraday-ai-1min-v4.1-expanded-paper"),
        ("confirm2_cooldown30", "intraday-ai-1min-v5.1-low-turnover-paper"),
    ],
)
def test_expanded_trade_filter_has_distinct_version(config, risk, trade_filter, version):
    settings = probe_config(config, intraday_probe_profile="expanded_paper")
    if trade_filter is not None:
        settings["intraday_trade_filter"] = trade_filter
    strategy = IntradayAIStrategy(settings, risk)
    assert strategy.version == version
    assert strategy.config == settings


def test_expanded_cost_cover_guard_has_distinct_version(config, risk):
    settings = probe_config(
        config,
        intraday_probe_profile="expanded_paper",
        intraday_trade_filter="confirm2_cooldown30",
        intraday_require_cost_cover=True,
        intraday_entry_cooldown_seconds=1800,
    )
    strategy = IntradayAIStrategy(settings, risk)
    assert strategy.version == "intraday-ai-1min-v5.2-economic-low-turnover-paper"
    assert strategy.require_entry_cost_cover is True


@pytest.mark.parametrize("value", [True, False, None, "unknown", [], {}])
def test_direct_strategy_rejects_invalid_trade_filter(config, risk, value):
    with pytest.raises(AgentError, match="intraday_trade_filter"):
        IntradayAIStrategy(probe_config(config, intraday_trade_filter=value), risk)


def test_direct_strategy_rejects_trade_filter_on_small_probe(config, risk):
    with pytest.raises(AgentError, match="requires intraday_ai expanded_paper capped_probe"):
        IntradayAIStrategy(probe_config(config, intraday_trade_filter="confirm2_cooldown30"), risk)


def test_probe_allows_confirmed_small_signal_without_claiming_cost_coverage(config, market, portfolio):
    portfolio = replace(portfolio, equity_usd=D(100000), cash_usd=D(100000))
    costs = {"fee_buffer_bps": D(30), "slippage_bps": D(20)}
    old = IntradayAIStrategy(config, costs)
    features = old._features(market, bars(), now=NOW)
    assert old.parse_output(output(), market, portfolio, features, NOW, now=NOW).rating == "REVIEW"
    new = IntradayAIStrategy(probe_config(config), costs)
    decision = new.parse_output(output(), market, portfolio, features, NOW, now=NOW)
    assert decision.actionable and decision.evaluation_eligible
    assert decision.target_position_pct * portfolio.equity_usd == D(10)
    assert any("not predicted profit" in evidence for evidence in decision.evidence)
    weak = new.parse_output(output(), market, portfolio, replace(features, momentum_score=1), NOW, now=NOW)
    assert weak.rating == "REVIEW" and not weak.actionable


@pytest.mark.parametrize("value", [None, 0, -1, "NaN", 11])
def test_invalid_probe_position_cap_fails_closed(config, risk, value):
    with pytest.raises(AgentError):
        IntradayAIStrategy(probe_config(config, intraday_probe_max_position_usd=value), risk)


@pytest.mark.parametrize("value", [None, 0, -1, "NaN", ".151"])
def test_invalid_probe_cost_budget_fails_closed(config, risk, value):
    with pytest.raises(AgentError):
        IntradayAIStrategy(probe_config(config, intraday_probe_cost_budget_usd=value), risk)


@pytest.mark.parametrize("value", [[], {}, None, True])
def test_policy_type_has_clear_validation_error(config, risk, value):
    with pytest.raises(AgentError, match="intraday_entry_policy"):
        IntradayAIStrategy({**config, "intraday_entry_policy": value}, risk)


def test_model_review_reason_preserved_and_malformed_output_not_observation(config, risk, market, portfolio):
    strategy = IntradayAIStrategy(probe_config(config), risk)
    features = strategy._features(market, bars(), now=NOW)
    decision = strategy.parse_output(output("REVIEW"), market, portfolio, features, NOW, now=NOW)
    assert output()["summary"] in decision.reason
    assert decision.evaluation_eligible and not decision.actionable
    assert not strategy.parse_output({}, market, portfolio, features, NOW, now=NOW).evaluation_eligible
    assert not strategy.parse_output(
        output(), market, portfolio, features, NOW, now=NOW + timedelta(minutes=5)
    ).evaluation_eligible


def test_probe_guard_counts_holdings_and_rechecks_spread(config, market, portfolio):
    from crypto_agent.models import OrderIntent
    from crypto_agent.risk.intraday import validate_intraday_order

    settings = probe_config(config, intraday_probe_cost_budget_usd=D(".102"))
    costs = {"fee_buffer_bps": D(30), "slippage_bps": D(20)}
    order = OrderIntent("BTC/USD", "buy", D(".049"), "probe-test", D("100.61"), D(5), D(".015"))
    held = replace(portfolio, positions=(Position("BTC/USD", D(".05"), D(100), D(".05")),))
    assert validate_intraday_order(order, market, held, settings, costs).allowed
    assert not validate_intraday_order(
        replace(order, quantity=D(".06")), market, held, settings, costs
    ).allowed
    wider = replace(market, bid=D("100.30"))
    assert not validate_intraday_order(order, wider, held, settings, costs).allowed
    assert validate_intraday_order(replace(order, side="sell"), wider, held, settings, costs).allowed


def test_probe_cost_cover_opt_in_blocks_low_bps_signal(config, market, portfolio):
    portfolio = replace(portfolio, equity_usd=D(100000), cash_usd=D(100000))
    costs = {"fee_buffer_bps": D(30), "slippage_bps": D(20)}
    settings = probe_config(
        config,
        intraday_probe_profile="expanded_paper",
        intraday_trade_filter="confirm2_cooldown30",
        intraday_require_cost_cover=True,
        intraday_entry_cooldown_seconds=1800,
    )
    strategy = IntradayAIStrategy(settings, costs)
    features = strategy._features(market, bars(), now=NOW)
    decision = strategy.parse_output(output(), market, portfolio, features, NOW, now=NOW)
    assert decision.rating == "REVIEW"
    assert not decision.actionable
    assert "round-trip cost" in decision.evidence[0]


def test_entry_economics_failure_reasons_are_tuple(config, market, portfolio):
    from crypto_agent.models import OrderIntent, TradeDecision
    from crypto_agent.risk.intraday import validate_entry_economics

    settings = probe_config(
        config,
        intraday_probe_profile="expanded_paper",
        intraday_trade_filter="confirm2_cooldown30",
        intraday_require_cost_cover=True,
        intraday_entry_cooldown_seconds=1800,
        intraday_lookback_bars=60,
        intraday_min_bars=45,
        intraday_max_bar_age_seconds=180,
        intraday_momentum_threshold_bps=D(2),
    )
    order = OrderIntent("BTC/USD", "buy", D(".01"), "economics", D("100.61"), D(1), D(".003"))
    decision = TradeDecision(
        "BTC/USD",
        D(".01"),
        "test",
        NOW + timedelta(seconds=120),
        NOW,
        "Buy",
        "v5.2",
        "test",
        ("test",),
    )
    result = validate_entry_economics(
        order, market, decision, (), settings, {"fee_buffer_bps": D(30), "slippage_bps": D(20)}
    )
    assert not result.allowed
    assert isinstance(result.reasons, tuple)
    assert result.reasons == ("Intraday minute bars are missing",)
