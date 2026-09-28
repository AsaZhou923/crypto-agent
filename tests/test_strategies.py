"""Offline adapter contract tests; these do not claim a live model integration."""

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from crypto_agent.models import (
    UPSTREAM_COMMIT,
    AgentError,
    BrokerOrder,
    MarketSnapshot,
    PortfolioSnapshot,
    Position,
)
from crypto_agent.strategies import tradingagents as adapter
from crypto_agent.strategies.baseline import BaselineStrategy
from crypto_agent.strategies.tradingagents import TradingAgentsStrategy, verify_upstream

D = Decimal
NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


@pytest.fixture
def config():
    return {
        "asset_type": "crypto",
        "analysis_symbol": "BTC-USD",
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_repository": "/test/TradingAgents",
        "llm_provider": "openai",
        "deep_think_llm": "test-deep",
        "quick_think_llm": "test-quick",
        "selected_analysts": ["market"],
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "output_language": "Chinese",
        "decision_profile": "balanced",
        "timeout_seconds": 2,
        "decision_ttl_seconds": 300,
        "rating_target_pct": {
            "Buy": D("0.10"),
            "Overweight": D("0.05"),
            "Underweight": D("0.02"),
            "Sell": D(0),
        },
        "baseline_buy_below_usd": D(60000),
        "baseline_target_pct": D("0.10"),
    }


@pytest.fixture
def market():
    return MarketSnapshot("BTC/USD", D(50000), NOW, D(49999), D(50001), "test-fixture")


@pytest.fixture
def portfolio():
    return PortfolioSnapshot(
        D(9000), D(10000), (Position("BTC/USD", D("0.02"), D(45000), D("0.02")),), NOW, D(9000)
    )


def output(rating="Buy"):
    return {
        "signal": rating,
        "final_trade_decision": (
            f"**Rating**: {rating}\n\n"
            "**Executive Summary**: Follow the fixed allocation limits.\n\n"
            "**Investment Thesis**: Test evidence: price is above the 20-day average.\n\n"
            "**Price Target**: not provided\n\n**Time Horizon**: not provided"
        ),
        "market_report": "Explicit test fixture: 20-day average 49000 USD; current 50000 USD.",
    }


@pytest.mark.parametrize(
    ("rating", "target", "actionable"),
    [
        ("Buy", "0.10", True),
        ("Overweight", "0.10", True),
        ("Hold", "0.10", False),
        ("Underweight", "0.02", True),
        ("Sell", "0", True),
    ],
)
def test_all_five_ratings_have_explicit_directional_targets(
    config, market, portfolio, rating, target, actionable
):
    decision = TradingAgentsStrategy(config).parse_output(output(rating), market, portfolio, NOW, now=NOW)
    assert decision.rating == rating
    assert decision.target_position_pct == D(target)
    assert decision.actionable is actionable
    assert decision.strategy_version.endswith(UPSTREAM_COMMIT)
    assert "test-deep" in decision.model
    assert len(decision.evidence) == 4


def test_underweight_never_opens_a_new_position(config, market, portfolio):
    flat = replace(portfolio, positions=())
    result = TradingAgentsStrategy(config).parse_output(output("Underweight"), market, flat, NOW, now=NOW)
    assert result.target_position_pct == 0


def test_eth_decision_uses_eth_holding_and_preserves_symbol(config, portfolio):
    market = MarketSnapshot("ETH/USD", D(2500), NOW, D(2499), D(2501), "test-fixture")
    mixed = replace(
        portfolio,
        positions=(
            Position("BTC/USD", D("0.02"), D(45000), D("0.02")),
            Position("ETH/USD", D("0.4"), D(2400), D("0.4")),
        ),
    )
    decision = TradingAgentsStrategy(config).parse_output(output("Hold"), market, mixed, NOW, now=NOW)
    assert decision.symbol == "ETH/USD"
    assert decision.target_position_pct == D("0.1")


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        {},
        {"signal": []},
        {"signal": "BUY"},
        {"signal": "REVIEW"},
        {"signal": "Buy", "final_trade_decision": "We should Buy on strength.", "market_report": "test"},
        {"signal": "Buy", "final_trade_decision": "Rating: Buy", "market_report": "test"},
    ],
)
def test_malformed_and_heuristic_outputs_are_non_actionable(config, market, portfolio, invalid):
    decision = TradingAgentsStrategy(config).parse_output(invalid, market, portfolio, NOW, now=NOW)
    assert not decision.actionable
    assert decision.rating == "REVIEW"
    assert decision.target_position_pct is None


def test_conflicting_explicit_and_upstream_ratings_are_rejected(config, market, portfolio):
    data = output("Buy")
    data["signal"] = "Sell"
    decision = TradingAgentsStrategy(config).parse_output(data, market, portfolio, NOW, now=NOW)
    assert not decision.actionable


def test_repeated_rating_fields_are_ambiguous(config, market, portfolio):
    data = output("Buy")
    data["final_trade_decision"] += "\n**Rating**: Sell"
    assert not TradingAgentsStrategy(config).parse_output(data, market, portfolio, NOW, now=NOW).actionable


@pytest.mark.parametrize("missing", ["", "   ", None, "unavailable", "not provided"])
def test_market_evidence_is_required(config, market, portfolio, missing):
    data = output()
    data["market_report"] = missing
    assert not TradingAgentsStrategy(config).parse_output(data, market, portfolio, NOW, now=NOW).actionable


def test_decision_expiry_is_anchored_to_analysis_start(config, market, portfolio):
    decision = TradingAgentsStrategy(config).parse_output(
        output(), market, portfolio, NOW, now=NOW + timedelta(seconds=300)
    )
    assert not decision.actionable
    assert decision.expires_at == NOW + timedelta(seconds=300)


def test_open_orders_block_model_call(config, market, portfolio, monkeypatch):
    pending = BrokerOrder("1", "client-1", "BTC/USD", "buy", D("0.01"), D(0), None, "new", NOW)
    monkeypatch.setattr(
        adapter, "verify_upstream", lambda *args: pytest.fail("Should not start model with pending orders")
    )
    decision = TradingAgentsStrategy(config).decide(market, replace(portfolio, open_orders=(pending,)))
    assert decision.rating == "REVIEW"
    assert not decision.actionable


def test_timeout_never_produces_order_or_leaks_error(config, market, portfolio, monkeypatch):
    monkeypatch.setattr(adapter, "verify_upstream", lambda *args: None)

    def timed_out(*args, **kwargs):
        assert kwargs["timeout"] == 2
        raise subprocess.TimeoutExpired("provider", 2, output="secret-provider-output")

    monkeypatch.setattr(adapter.subprocess, "run", timed_out)
    decision = TradingAgentsStrategy(config).decide(market, portfolio)
    assert decision.rating == "REVIEW"
    assert "timed out" in decision.reason
    assert "secret" not in decision.reason


@pytest.mark.skipif(os.name != "posix", reason="Uses POSIX process existence check")
def test_real_worker_process_is_killed_and_reaped_on_deadline(
    config, market, portfolio, monkeypatch, tmp_path
):
    worker = tmp_path / "_tradingagents_worker.py"
    worker.write_text(
        "import json, os, time\n"
        "from pathlib import Path\n"
        "root = Path(__file__).parent\n"
        "(root / 'started.json').write_text(json.dumps({'pid': os.getpid(), 'cwd': os.getcwd()}))\n"
        "time.sleep(30)\n"
        "(root / 'finished').touch()\n"
    )
    monkeypatch.setattr(adapter, "__file__", str(tmp_path / "tradingagents.py"))
    monkeypatch.setattr(adapter, "verify_upstream", lambda *args: None)
    strategy = TradingAgentsStrategy({**config, "timeout_seconds": 1})
    started_at = time.monotonic()
    decision = strategy.decide(market, portfolio)
    elapsed = time.monotonic() - started_at

    # The marker proves a real Python worker started. Its absent PID proves
    # timeout killed and reaped it, rather than leaving inference in the background.
    child = json.loads((tmp_path / "started.json").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child["pid"], 0)
    assert elapsed < 10
    assert not (tmp_path / "finished").exists()
    assert not Path(child["cwd"]).exists()
    assert not decision.actionable
    assert decision.target_position_pct is None
    assert decision.rating == "REVIEW"
    assert "timed out" in decision.reason


@pytest.mark.parametrize("backend_url", [None, "https://llm.example/v1"])
def test_worker_receives_portfolio_and_only_model_credentials(
    config, market, portfolio, monkeypatch, backend_url
):
    monkeypatch.setattr(adapter, "verify_upstream", lambda *args: None)
    monkeypatch.setattr(adapter, "utcnow", lambda: NOW)
    monkeypatch.setenv("ALPACA_API_SECRET", "do-not-forward")
    monkeypatch.setenv("UNRELATED_TOKEN", "do-not-forward-either")
    monkeypatch.setenv("OPENAI_API_KEY", "test-model-key")

    def run(*args, **kwargs):
        payload = json.loads(kwargs["input"])
        assert "test-model-key" not in kwargs["input"]
        assert payload["config"]["backend_url"] == backend_url
        assert payload["ticker"] == "BTC-USD"
        assert payload["portfolio"]["positions"][0]["ticker"] == "BTC-USD"
        assert payload["portfolio"]["positions"][0]["quantity"] == "0.02"
        assert payload["portfolio"]["cash"] == "9000"
        assert "50000" in payload["snapshot_context"]
        assert "Decision profile: balanced" in payload["snapshot_context"]
        assert kwargs["env"]["OPENAI_API_KEY"] == "test-model-key"
        assert "ALPACA_API_SECRET" not in kwargs["env"]
        assert "UNRELATED_TOKEN" not in kwargs["env"]
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        assert kwargs["env"]["PYTHON_DOTENV_DISABLED"] == "1"
        return SimpleNamespace(returncode=0, stdout=json.dumps(output()))

    monkeypatch.setattr(adapter.subprocess, "run", run)
    assert TradingAgentsStrategy({**config, "backend_url": backend_url}).decide(market, portfolio).actionable


def test_short_term_profile_reaches_worker_without_changing_hard_targets(
    config, market, portfolio, monkeypatch
):
    monkeypatch.setattr(adapter, "verify_upstream", lambda *args: None)

    def run(*args, **kwargs):
        payload = json.loads(kwargs["input"])
        context = payload["snapshot_context"]
        assert "one-to-three-day holding horizon" in context
        assert "small probe" in context
        assert "short selling remains forbidden" in context
        return SimpleNamespace(returncode=0, stdout=json.dumps(output("Overweight")))

    monkeypatch.setattr(adapter.subprocess, "run", run)
    short_term = {
        **config,
        "decision_profile": "short_term_small",
        "rating_target_pct": {
            "Buy": D("0.0005"),
            "Overweight": D("0.0003"),
            "Underweight": D("0.0001"),
            "Sell": D(0),
        },
    }
    decision = TradingAgentsStrategy(short_term).decide(market, replace(portfolio, positions=()))
    assert decision.rating == "Overweight"
    assert decision.target_position_pct == D("0.0003")


def test_unknown_decision_profile_is_rejected(config):
    with pytest.raises(AgentError, match="decision profile"):
        TradingAgentsStrategy({**config, "decision_profile": "force-trades"})


def test_worker_payload_uses_selected_eth_ticker_and_mixed_portfolio(config, portfolio, monkeypatch):
    market = MarketSnapshot("ETH/USD", D(2500), NOW, D(2499), D(2501), "test-fixture")
    portfolio = replace(
        portfolio,
        positions=portfolio.positions + (Position("ETH/USD", D(".4"), D(2400), D(".4")),),
    )
    monkeypatch.setattr(adapter, "verify_upstream", lambda *args: None)
    monkeypatch.setattr(adapter, "utcnow", lambda: NOW)

    def run(*args, **kwargs):
        payload = json.loads(kwargs["input"])
        assert payload["ticker"] == "ETH-USD"
        assert [item["ticker"] for item in payload["portfolio"]["positions"]] == [
            "BTC-USD",
            "ETH-USD",
        ]
        assert "ETH/USD quote 2500" in payload["snapshot_context"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(output("Hold")))

    monkeypatch.setattr(adapter.subprocess, "run", run)
    decision = TradingAgentsStrategy(config).decide(market, portfolio)
    assert decision.symbol == "ETH/USD" and decision.rating == "Hold"


def test_invalid_worker_response_fails_closed(config, market, portfolio, monkeypatch):
    monkeypatch.setattr(adapter, "verify_upstream", lambda *args: None)
    monkeypatch.setattr(
        adapter.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="not json")
    )
    assert not TradingAgentsStrategy(config).decide(market, portfolio).actionable


@pytest.mark.parametrize(
    "dirty,revision", [(" M tradingagents/portfolio.py", UPSTREAM_COMMIT), ("", "wrong-commit")]
)
def test_unreviewed_upstream_source_is_rejected(tmp_path, monkeypatch, dirty, revision):
    source = tmp_path / "tradingagents/graph/trading_graph.py"
    source.parent.mkdir(parents=True)
    source.touch()
    results = iter([SimpleNamespace(stdout=revision), SimpleNamespace(stdout=dirty)])
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: next(results))
    with pytest.raises(AgentError, match="pinned commit"):
        verify_upstream(tmp_path)


def test_worker_entry_point_sanitizes_errors(tmp_path):
    worker = Path(adapter.__file__).with_name("_tradingagents_worker.py")
    result = subprocess.run(
        [sys.executable, str(worker)],
        input='{"secret":"not-for-logs"}',
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=5,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"error": "AI worker failed"}
    assert result.stderr == ""
    assert "not-for-logs" not in result.stdout


def test_worker_redacts_json_escaped_credentials(monkeypatch, capsys):
    from io import StringIO

    from crypto_agent.strategies import _tradingagents_worker as worker

    secret = 'test-credential-with-"quote"-and-\\slash'
    access_key = "TEST-AWS-ACCESS-IDENTIFIER"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", access_key)
    monkeypatch.setattr(sys, "stdin", StringIO("{}"))
    monkeypatch.setattr(worker.logging, "disable", lambda _: None)
    monkeypatch.setattr(
        worker,
        "run",
        lambda _: {
            "signal": "REVIEW",
            "final_trade_decision": f"{secret} {access_key}",
            "market_report": "test",
        },
    )
    assert worker.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["final_trade_decision"] == "[REDACTED] [REDACTED]"


def test_worker_explicitly_uses_crypto_mode_and_rendered_account_context(monkeypatch, tmp_path):
    from crypto_agent.strategies import _tradingagents_worker as worker

    class FakePortfolioContext:
        @classmethod
        def model_validate(cls, values):
            context = cls()
            for name, value in values.items():
                setattr(context, name, value)
            return context

        def render(self, ticker):
            return f"Holdings for {ticker}: {self.positions}; cash {self.cash}"

    class FakeGraph:
        def __init__(self, **kwargs):
            assert kwargs["selected_analysts"] == ["market"]
            assert kwargs["debug"] is False
            assert kwargs["config"]["checkpoint_enabled"] is False
            assert kwargs["config"]["memory_log_path"] == str(tmp_path / "memory.md")
            assert kwargs["config"]["backend_url"] == "https://llm.example/v1"

        def propagate(self, ticker, date, *, asset_type, portfolio):
            assert ticker == "BTC-USD"
            assert date == "2026-09-19"
            assert asset_type == "crypto"
            assert "BTC-USD" in portfolio.render(ticker)
            assert "authoritative broker quote" in portfolio.render(ticker)
            return {"final_trade_decision": "Rating: Hold", "market_report": "test"}, "Hold"

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "tradingagents.default_config", SimpleNamespace(DEFAULT_CONFIG={}))
    monkeypatch.setitem(
        sys.modules, "tradingagents.graph.trading_graph", SimpleNamespace(TradingAgentsGraph=FakeGraph)
    )
    monkeypatch.setitem(
        sys.modules, "tradingagents.portfolio", SimpleNamespace(PortfolioContext=FakePortfolioContext)
    )
    result = worker.run(
        {
            "repository": str(tmp_path),
            "config": {"backend_url": "https://llm.example/v1"},
            "selected_analysts": ["market"],
            "trade_date": "2026-09-19",
            "ticker": "BTC-USD",
            "portfolio": {"positions": [], "cash": "10000"},
            "snapshot_context": "authoritative broker quote",
        }
    )
    assert result["signal"] == "Hold"


def test_baseline_threshold_and_metadata(config, market, portfolio, monkeypatch):
    from crypto_agent.strategies import baseline

    monkeypatch.setattr(baseline, "utcnow", lambda: NOW)
    strategy = BaselineStrategy(config)
    assert strategy.decide(market, portfolio).target_position_pct == D("0.10")
    assert strategy.decide(replace(market, price=D(60000)), portfolio).target_position_pct == D("0.10")
    result = strategy.decide(replace(market, price=D(60001)), portfolio)
    assert result.target_position_pct == 0
    assert result.model == "none"
    assert result.expires_at == NOW + timedelta(seconds=300)


def test_baseline_missing_configuration_is_rejected():
    with pytest.raises(AgentError):
        BaselineStrategy({})
