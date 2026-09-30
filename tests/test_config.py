import shutil

import pytest
import yaml

from crypto_agent.config import load_settings
from crypto_agent.models import AgentError


@pytest.mark.parametrize("value", [True, 59, 3601, 300.5, "300"])
def test_invalid_automatic_interval(config, value):
    update(config, "paper.yaml", automatic_interval_seconds=value)
    with pytest.raises(AgentError, match="automatic_interval_seconds"):
        load_settings(root=config)


def test_cadence_and_batch_size_are_approval_bound(config):
    original = load_settings(root=config)
    update(config, "paper.yaml", automatic_interval_seconds=300, automatic_symbols_per_cycle=1)
    fast = load_settings(root=config)
    assert fast.paper["automatic_interval_seconds"] == 300
    assert fast.digest != original.digest
    update(config, "paper.yaml", automatic_symbols_per_cycle=2)
    with pytest.raises(AgentError, match="automatic_symbols_per_cycle"):
        load_settings(root=config)


@pytest.mark.parametrize("value", [1, "true", None])
def test_order_cap_optin_requires_boolean(config, value):
    update(config, "risk.yaml", cap_order_to_limit=value)
    with pytest.raises(AgentError, match="cap_order_to_limit"):
        load_settings(root=config)


def test_expanded_probe_requires_explicit_profile():
    from crypto_agent.config import validate_intraday_policy

    values = dict(
        intraday_entry_policy="capped_probe",
        intraday_probe_max_position_usd=5000,
        intraday_probe_cost_budget_usd=75,
    )
    with pytest.raises(AgentError):
        validate_intraday_policy(values)
    assert validate_intraday_policy({**values, "intraday_probe_profile": "expanded_paper"}) == "capped_probe"
    for key, value in (("intraday_probe_max_position_usd", 5001), ("intraday_probe_cost_budget_usd", 76)):
        with pytest.raises(AgentError):
            validate_intraday_policy({**values, "intraday_probe_profile": "expanded_paper", key: value})


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_LLM_BACKEND_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    shutil.copytree("config/demo", tmp_path / "config")
    return tmp_path


def update(root, file, **values):
    path = root / "config" / file
    data = yaml.safe_load(path.read_text())
    data.update(values)
    path.write_text(yaml.safe_dump(data))


def test_default_missing_limits_fails_closed():
    with pytest.raises(AgentError, match="Missing required risk limits"):
        load_settings()


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.test",
        "https://paper-api.alpaca.markets/../v2",
        "https://x@paper-api.alpaca.markets",
    ],
)
def test_reject_non_paper_url(config, url):
    update(config, "paper.yaml", base_url=url)
    with pytest.raises(AgentError, match="Paper endpoint"):
        load_settings(root=config)


def test_reject_unreviewed_market_data_source(config):
    update(config, "paper.yaml", market_data_source="latest_quotes")
    with pytest.raises(AgentError, match="market_data_source"):
        load_settings(root=config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_position_pct", 1.1),
        ("fee_buffer_bps", 0),
        ("slippage_bps", -1),
        ("max_data_age_seconds", "NaN"),
        ("allow_short", True),
        ("max_daily_loss_usd", None),
    ],
)
def test_invalid_limits(config, field, value):
    update(config, "risk.yaml", **{field: value})
    with pytest.raises(AgentError):
        load_settings(root=config)


def test_env_does_not_override_existing_secret_or_enter_summary(config, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "preexisting-private-value")
    (config / ".env").write_text("ALPACA_API_KEY=local-different-value\n")
    settings = load_settings(root=config)
    import os

    assert os.environ["ALPACA_API_KEY"] == "preexisting-private-value"
    assert "preexisting-private-value" not in str(settings.summary)
    assert "local-different-value" not in str(settings.summary)


def test_model_overrides_and_database_isolation(config, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("TRADINGAGENTS_DEEP_THINK_LLM", "my-model")
    settings = load_settings(root=config, mode="offline")
    assert settings.strategy["llm_provider"] == "openai"
    assert settings.strategy["deep_think_llm"] == "my-model"
    assert "offline" in str(settings.database_path)


def test_unknown_sensitive_yaml_field_rejected(config):
    update(config, "strategy.yaml", api_key="never-save-this-value")
    with pytest.raises(AgentError) as error:
        load_settings(root=config)
    assert "never-save-this-value" not in str(error.value)


def test_reject_accidental_live_env(config, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "false")
    with pytest.raises(AgentError, match="must be true"):
        load_settings(root=config)


def test_backend_url_defaults_to_provider_endpoint(config):
    assert load_settings(root=config).strategy["backend_url"] is None


def test_unknown_decision_profile_is_rejected(config):
    update(config, "strategy.yaml", decision_profile="force-trades")
    with pytest.raises(AgentError, match="decision_profile"):
        load_settings(root=config)


def test_intraday_strategy_requires_complete_valid_minute_configuration(config):
    update(
        config,
        "strategy.yaml",
        name="intraday_ai",
        llm_provider="openai",
        deep_think_llm="test-model",
        quick_think_llm="test-model",
        decision_profile="intraday_10m",
        intraday_lookback_bars=60,
        intraday_min_bars=45,
        intraday_max_bar_age_seconds=180,
        intraday_momentum_threshold_bps=2,
        intraday_entry_score=2,
        intraday_entry_policy="cost_cover",
    )
    settings = load_settings(root=config)
    assert settings.strategy["name"] == "intraday_ai"
    assert settings.strategy["intraday_momentum_threshold_bps"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("intraday_min_bars", 30),
        ("intraday_lookback_bars", 240),
        ("intraday_max_bar_age_seconds", 0),
        ("intraday_momentum_threshold_bps", 0),
        ("intraday_entry_score", 5),
        ("intraday_entry_policy", "unknown"),
    ],
)
def test_intraday_strategy_rejects_unsafe_windows(config, field, value):
    values = {
        "name": "intraday_ai",
        "llm_provider": "openai",
        "deep_think_llm": "test-model",
        "quick_think_llm": "test-model",
        "decision_profile": "intraday_10m",
        "intraday_lookback_bars": 60,
        "intraday_min_bars": 45,
        "intraday_max_bar_age_seconds": 180,
        "intraday_momentum_threshold_bps": 2,
        "intraday_entry_score": 2,
        "intraday_entry_policy": "cost_cover",
        field: value,
    }
    update(config, "strategy.yaml", **values)
    with pytest.raises(AgentError):
        load_settings(root=config)


def test_backend_url_is_normalized_and_part_of_config_digest(config):
    initial = load_settings(root=config)
    update(config, "strategy.yaml", backend_url="https://llm.example/v1///")
    custom = load_settings(root=config)
    assert custom.strategy["backend_url"] == "https://llm.example/v1"
    assert custom.summary["strategy"]["backend_url"] == "https://llm.example/v1"
    assert custom.digest != initial.digest
    update(config, "strategy.yaml", backend_url="https://llm.example/v1")
    assert load_settings(root=config).digest == custom.digest


@pytest.mark.parametrize(
    "provider,generic,specific,expected",
    [
        ("openai", "https://openai.example/v1", None, "https://openai.example/v1"),
        ("openai", "https://openai.example/v1", "https://chosen.example/v1/", "https://chosen.example/v1"),
        ("anthropic", "https://openai.example/v1", None, "https://yaml.example/v1"),
        ("anthropic", "https://openai.example/v1", "https://chosen.example/v1", "https://chosen.example/v1"),
        ("openai", "", "", "https://yaml.example/v1"),
    ],
)
def test_backend_url_override_precedence(config, monkeypatch, provider, generic, specific, expected):
    update(config, "strategy.yaml", llm_provider=provider, backend_url="https://yaml.example/v1")
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", provider)
    monkeypatch.setenv("OPENAI_BASE_URL", generic)
    if specific is not None:
        monkeypatch.setenv("TRADINGAGENTS_LLM_BACKEND_URL", specific)
    assert load_settings(root=config).strategy["backend_url"] == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://llm.example/v1",
        "https:///v1",
        "https://secret-user:secret-password@llm.example/v1",
        "https://llm.example/v1?api_key=secret-key",
        "https://llm.example/v1?",
        "https://llm.example/v1#secret-fragment",
        "https://llm.example/v1#",
        " https://llm.example/v1",
        "https://llm.example/v1\n",
        "https://llm.example/a b",
        "https://llm.example/\x00",
        "https://llm.example\\evil/v1",
        "https://llm.example:invalid/v1",
        "https://llm.example:70000/v1",
        "https://[invalid/v1",
        42,
    ],
)
def test_unsafe_backend_url_rejected_without_echoing_secrets(config, url):
    update(config, "strategy.yaml", backend_url=url)
    with pytest.raises(AgentError, match="backend_url must be an HTTPS URL") as error:
        load_settings(root=config)
    assert "secret-" not in str(error.value)


@pytest.mark.parametrize("variable", ["TRADINGAGENTS_LLM_BACKEND_URL", "OPENAI_BASE_URL"])
def test_environment_backend_url_is_validated(config, monkeypatch, variable):
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai")
    monkeypatch.setenv(variable, "https://secret-user@llm.example/v1")
    with pytest.raises(AgentError, match="backend_url must be an HTTPS URL"):
        load_settings(root=config)


def test_three_symbol_universe_is_allowed_and_must_match_risk(config):
    symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
    update(config, "paper.yaml", symbols=symbols)
    update(
        config,
        "risk.yaml",
        allowed_symbols=symbols,
        max_total_position_pct=0.30,
        price_bounds_usd={symbol: {"min": 1, "max": 1000000} for symbol in symbols},
    )
    settings = load_settings(root=config)
    assert settings.paper["symbols"] == symbols
    assert settings.risk["allowed_symbols"] == symbols


def test_xrp_can_replace_a_stale_configured_symbol(config):
    symbols = ["BTC/USD", "XRP/USD", "DOGE/USD"]
    update(config, "paper.yaml", symbols=symbols)
    update(
        config,
        "risk.yaml",
        allowed_symbols=symbols,
        max_total_position_pct=0.30,
        min_price_usd=0.001,
        price_bounds_usd={
            "BTC/USD": {"min": 100, "max": 1000000},
            "XRP/USD": {"min": 0.01, "max": 1000},
            "DOGE/USD": {"min": 0.001, "max": 10},
        },
    )
    settings = load_settings(root=config)
    assert settings.paper["symbols"] == symbols
    assert str(settings.risk["price_bounds_usd"]["XRP/USD"]["min"]) == "0.01"


@pytest.mark.parametrize(
    "paper_symbols,risk_symbols",
    [
        (["BTC/USD", "ETH/USD", "SOL/USD", "BTC/USD"], ["BTC/USD"]),
        (["BTC/USD", "BTC/USD"], ["BTC/USD", "BTC/USD"]),
        (["BTC/USD", "DOGE/USD"], ["BTC/USD", "DOGE/USD"]),
        (["BTC/USD", "ETH/USD"], ["BTC/USD"]),
    ],
)
def test_invalid_or_mismatched_symbol_universe_is_rejected(config, paper_symbols, risk_symbols):
    update(config, "paper.yaml", symbols=paper_symbols)
    update(config, "risk.yaml", allowed_symbols=risk_symbols)
    with pytest.raises(AgentError):
        load_settings(root=config)


@pytest.mark.parametrize("field", ["max_position_notional_usd", "max_total_position_notional_usd"])
@pytest.mark.parametrize("value", [0, -1, "NaN", True])
def test_invalid_absolute_dollar_limits(config, field, value):
    update(config, "risk.yaml", **{field: value})
    with pytest.raises(AgentError):
        load_settings(root=config)


def test_absolute_dollar_limits_approval_bound(config):
    original = load_settings(root=config)
    update(config, "risk.yaml", max_position_notional_usd=5000, max_total_position_notional_usd=10000)
    assert load_settings(root=config).digest != original.digest
    update(config, "risk.yaml", max_position_notional_usd=10001)
    with pytest.raises(AgentError, match="Per-symbol dollar cap"):
        load_settings(root=config)


@pytest.fixture
def expanded_config(config):
    update(
        config,
        "paper.yaml",
        symbols=["BTC/USD", "XRP/USD"],
        automatic_interval_seconds=300,
        automatic_symbols_per_cycle=2,
    )
    update(
        config,
        "strategy.yaml",
        intraday_probe_profile="expanded_paper",
        intraday_entry_policy="capped_probe",
        intraday_probe_max_position_usd=5000,
        intraday_probe_cost_budget_usd=75,
        baseline_target_pct=0.05,
        rating_target_pct={"Buy": 0.05, "Overweight": 0.03, "Underweight": 0.01, "Sell": 0},
    )
    update(
        config,
        "risk.yaml",
        allowed_symbols=["BTC/USD", "XRP/USD"],
        price_bounds_usd={"BTC/USD": {"min": 100, "max": 1000000}, "XRP/USD": {"min": 0.01, "max": 1000}},
        max_position_pct=0.05,
        max_total_position_pct=0.10,
        max_order_notional_usd=500,
        max_daily_loss_usd=500,
        max_position_notional_usd=5000,
        max_total_position_notional_usd=10000,
    )
    return config


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_position_pct", 0.051),
        ("max_total_position_pct", 0.101),
        ("max_order_notional_usd", 501),
        ("max_daily_loss_usd", 501),
        ("max_position_notional_usd", 5001),
        ("max_total_position_notional_usd", 10001),
    ],
)
def test_expanded_profile_rejects_unauthorized_limits(expanded_config, field, value):
    settings = load_settings(root=expanded_config, mode="paper")
    assert settings.paper["automatic_symbols_per_cycle"] == 2
    update(expanded_config, "risk.yaml", **{field: value})
    with pytest.raises(AgentError, match="exceeds authorized"):
        load_settings(root=expanded_config, mode="paper")


@pytest.mark.parametrize("field", ["max_position_notional_usd", "max_total_position_notional_usd"])
def test_expanded_profile_requires_absolute_limits(expanded_config, field):
    path = expanded_config / "config" / "risk.yaml"
    data = yaml.safe_load(path.read_text())
    del data[field]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(AgentError, match="exceeds authorized"):
        load_settings(root=expanded_config, mode="paper")


@pytest.fixture
def filtered_config(expanded_config):
    update(
        expanded_config,
        "strategy.yaml",
        name="intraday_ai",
        llm_provider="openai",
        deep_think_llm="test-model",
        quick_think_llm="test-model",
        decision_profile="intraday_10m",
        intraday_lookback_bars=60,
        intraday_min_bars=45,
        intraday_max_bar_age_seconds=180,
        intraday_momentum_threshold_bps=2,
        intraday_entry_score=2,
    )
    return expanded_config


def test_trade_filter_defaults_to_none_without_changing_legacy_summary(filtered_config):
    from crypto_agent.config import validate_intraday_trade_filter

    original = load_settings(root=filtered_config)
    assert validate_intraday_trade_filter(original.strategy) == "none"
    assert "intraday_trade_filter" not in original.summary["strategy"]
    update(filtered_config, "strategy.yaml", intraday_trade_filter="confirm2_cooldown30")
    filtered = load_settings(root=filtered_config)
    assert filtered.strategy["intraday_trade_filter"] == "confirm2_cooldown30"
    assert filtered.digest != original.digest
    assert filtered.paper == original.paper and filtered.risk == original.risk
    assert {k: v for k, v in filtered.strategy.items() if k != "intraday_trade_filter"} == original.strategy


def test_entry_cost_cover_opt_in_changes_digest_only_when_explicit(filtered_config):
    original = load_settings(root=filtered_config)
    update(
        filtered_config,
        "strategy.yaml",
        intraday_trade_filter="confirm2_cooldown30",
        intraday_require_cost_cover=True,
        intraday_entry_cooldown_seconds=1800,
    )
    filtered = load_settings(root=filtered_config)
    assert "intraday_require_cost_cover" not in original.summary["strategy"]
    assert filtered.strategy["intraday_require_cost_cover"] is True
    assert filtered.strategy["intraday_entry_cooldown_seconds"] == 1800
    assert filtered.digest != original.digest
    economic_fields = {
        "intraday_trade_filter",
        "intraday_require_cost_cover",
        "intraday_entry_cooldown_seconds",
    }
    assert {k: v for k, v in filtered.strategy.items() if k not in economic_fields} == original.strategy


@pytest.mark.parametrize("value", ["unknown", True, False, None, 2, [], {}])
def test_trade_filter_rejects_invalid_values(filtered_config, value):
    update(filtered_config, "strategy.yaml", intraday_trade_filter=value)
    with pytest.raises(AgentError, match="intraday_trade_filter"):
        load_settings(root=filtered_config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "baseline"),
        ("name", "tradingagents"),
        ("intraday_probe_profile", "small"),
        ("intraday_entry_policy", "cost_cover"),
    ],
)
def test_trade_filter_rejects_mismatched_profile(filtered_config, field, value):
    update(filtered_config, "strategy.yaml", intraday_trade_filter="confirm2_cooldown30", **{field: value})
    with pytest.raises(AgentError, match="requires intraday_ai expanded_paper capped_probe"):
        load_settings(root=filtered_config)


def test_trade_filter_is_paper_only(filtered_config):
    update(filtered_config, "strategy.yaml", intraday_trade_filter="confirm2_cooldown30")
    with pytest.raises(AgentError, match="Paper-only"):
        load_settings(root=filtered_config, mode="offline")


@pytest.mark.parametrize("value", ["true", 1, None, [], {}])
def test_entry_cost_cover_rejects_invalid_values(filtered_config, value):
    update(
        filtered_config,
        "strategy.yaml",
        intraday_trade_filter="confirm2_cooldown30",
        intraday_require_cost_cover=value,
        intraday_entry_cooldown_seconds=1800,
    )
    with pytest.raises(AgentError, match="intraday_require_cost_cover"):
        load_settings(root=filtered_config)


@pytest.mark.parametrize("value", [59, 3601, "1800", True])
def test_entry_cooldown_rejects_invalid_values(filtered_config, value):
    update(
        filtered_config,
        "strategy.yaml",
        intraday_trade_filter="confirm2_cooldown30",
        intraday_require_cost_cover=True,
        intraday_entry_cooldown_seconds=value,
    )
    with pytest.raises(AgentError, match="intraday_entry_cooldown_seconds"):
        load_settings(root=filtered_config)


def test_entry_cost_cover_requires_entry_cooldown(filtered_config):
    update(
        filtered_config,
        "strategy.yaml",
        intraday_trade_filter="confirm2_cooldown30",
        intraday_require_cost_cover=True,
    )
    with pytest.raises(AgentError, match="requires intraday_entry_cooldown_seconds"):
        load_settings(root=filtered_config)


def test_entry_cooldown_requires_cost_cover_opt_in(filtered_config):
    update(
        filtered_config,
        "strategy.yaml",
        intraday_trade_filter="confirm2_cooldown30",
        intraday_entry_cooldown_seconds=1800,
    )
    with pytest.raises(AgentError, match="intraday_entry_cooldown_seconds"):
        load_settings(root=filtered_config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "baseline"),
        ("intraday_probe_profile", "small"),
        ("intraday_entry_policy", "cost_cover"),
        ("intraday_trade_filter", "none"),
    ],
)
def test_entry_cost_cover_rejects_mismatched_profile(filtered_config, field, value):
    values = {
        "intraday_trade_filter": "confirm2_cooldown30",
        "intraday_require_cost_cover": True,
        "intraday_entry_cooldown_seconds": 1800,
    }
    values[field] = value
    update(filtered_config, "strategy.yaml", **values)
    with pytest.raises(AgentError, match="requires intraday_ai expanded_paper capped_probe"):
        load_settings(root=filtered_config)


def test_entry_cost_cover_is_paper_only(filtered_config):
    update(
        filtered_config,
        "strategy.yaml",
        intraday_trade_filter="confirm2_cooldown30",
        intraday_require_cost_cover=True,
        intraday_entry_cooldown_seconds=1800,
    )
    with pytest.raises(AgentError, match="Paper-only"):
        load_settings(root=filtered_config, mode="offline")


def test_explicit_none_trade_filter_keeps_old_modes(config):
    update(config, "strategy.yaml", intraday_trade_filter="none")
    assert load_settings(root=config, mode="offline").strategy["intraday_trade_filter"] == "none"


def test_intraday_feature_contract_changes_approval_digest():
    from dataclasses import replace
    from hashlib import sha256
    from pathlib import Path

    from crypto_agent.models import dumps

    base = load_settings(Path("config/demo"), mode="offline")
    current = replace(base, strategy={**base.strategy, "name": "intraday_ai"})
    summary = current.summary
    assert summary.pop("intraday_feature_contract") == "contiguous-60-second-bars-v1"
    assert current.digest != sha256(dumps(summary).encode()).hexdigest()
    assert "intraday_feature_contract" not in base.summary
