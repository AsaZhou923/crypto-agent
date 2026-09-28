"""True minute horizons require consecutive provider bars; never fill missing bars."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from unittest.mock import Mock

import pytest

from crypto_agent.data.features import build_intraday_features
from crypto_agent.models import AgentError, MarketSnapshot, PortfolioSnapshot, PriceBar
from crypto_agent.strategies.intraday_ai import IntradayAIStrategy

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
MARKET = MarketSnapshot("XRP/USD", D(159), NOW, D("158.99"), D("159.01"), "synthetic-book")


def bars():
    return tuple(
        PriceBar(
            "XRP/USD",
            NOW - timedelta(minutes=60 - index),
            D(100 + index),
            D(101 + index),
            D(99 + index),
            D(100 + index),
            D(1),
            "synthetic-minute-bars",
        )
        for index in range(60)
    )


def features(data):
    return build_intraday_features(
        MARKET,
        data,
        lookback_bars=60,
        min_bars=45,
        max_age_seconds=180,
        momentum_threshold_bps=D(2),
        now=NOW,
    )


def gapped_bars(gap_seconds):
    data = bars()
    # Preserve count, values, final freshness and ordering while making a
    # provider hole in the selected window. Old tolerance admitted both holes.
    return tuple(
        replace(bar, observed_at=bar.observed_at - timedelta(seconds=gap_seconds - 60)) if index < 40 else bar
        for index, bar in enumerate(data)
    )


def test_regular_minutes_keep_exact_return_horizons_and_momentum():
    result = features(bars())
    assert result.bar_count == 60
    assert result.first_bar_at == NOW - timedelta(minutes=60)
    assert result.last_bar_at == NOW - timedelta(minutes=1)
    assert result.return_3m_bps == (D(159) / D(156) - 1) * D(10000)
    assert result.return_10m_bps == (D(159) / D(149) - 1) * D(10000)
    assert result.return_30m_bps == (D(159) / D(129) - 1) * D(10000)
    assert result.momentum_score == 4
    assert result.recent_volume_ratio == 1


@pytest.mark.parametrize("gap_seconds", [59, 61, 120, 180])
def test_selected_closed_bars_require_exact_sixty_second_steps(gap_seconds):
    with pytest.raises(AgentError, match="consecutive 60-second"):
        features(gapped_bars(gap_seconds))


def test_older_hole_outside_selected_window_does_not_change_features():
    old = replace(bars()[0], observed_at=NOW - timedelta(minutes=100))
    assert features((old, *bars())) == features(bars())


def test_still_open_bar_outside_used_window_does_not_trigger_gap_check():
    closed = tuple(replace(bar, observed_at=bar.observed_at - timedelta(minutes=1)) for bar in bars())
    still_open = replace(bars()[-1], observed_at=NOW)
    assert features((*closed, still_open)) == features(closed)


@pytest.mark.parametrize("value", [D(0), D(-1)])
def test_zero_or_negative_prices_still_rejected(value):
    data = (replace(bars()[0], close=value, low=value), *bars()[1:])
    with pytest.raises(AgentError, match="invalid OHLCV"):
        features(data)


def test_negative_volume_still_rejected_and_zero_volume_allowed():
    data = bars()
    with pytest.raises(AgentError, match="invalid OHLCV"):
        features((replace(data[0], volume=D(-1)), *data[1:]))
    assert features(tuple(replace(bar, volume=D(0)) for bar in data)).recent_volume_ratio is None


def test_duplicate_minute_still_rejected():
    data = bars()
    with pytest.raises(AgentError, match="duplicate timestamps"):
        features((*data[:-1], replace(data[-1], observed_at=data[-2].observed_at)))


def test_future_minute_still_rejected():
    data = bars()
    with pytest.raises(AgentError, match="future timestamp"):
        features((*data, replace(data[-1], observed_at=NOW + timedelta(seconds=6))))


def test_insufficient_closed_minutes_still_rejected():
    with pytest.raises(AgentError, match="Too few closed"):
        features(bars()[-44:])


@pytest.mark.parametrize("gap_seconds", [120, 180])
def test_gap_produces_ineligible_review_without_model_or_network(monkeypatch, gap_seconds):
    config = {
        "name": "intraday_ai",
        "asset_type": "crypto",
        "decision_profile": "intraday_10m",
        "llm_provider": "openai",
        "quick_think_llm": "synthetic-model",
        "timeout_seconds": 2,
        "decision_ttl_seconds": 120,
        "intraday_lookback_bars": 60,
        "intraday_min_bars": 45,
        "intraday_max_bar_age_seconds": 180,
        "intraday_momentum_threshold_bps": D(2),
        "intraday_entry_score": 2,
        "intraday_entry_policy": "capped_probe",
        "intraday_probe_profile": "expanded_paper",
        "intraday_probe_max_position_usd": D(5000),
        "intraday_probe_cost_budget_usd": D(75),
        "intraday_trade_filter": "confirm2_cooldown30",
        "rating_target_pct": {"Buy": D(".05"), "Overweight": D(".03"), "Underweight": D(".01"), "Sell": D(0)},
    }
    monkeypatch.setattr("crypto_agent.strategies.intraday_ai.utcnow", lambda: NOW)
    worker = Mock(side_effect=AssertionError("Model worker must not start for invalid bars"))
    monkeypatch.setattr("crypto_agent.strategies.intraday_ai.subprocess.run", worker)
    strategy = IntradayAIStrategy(config, {"fee_buffer_bps": D(30), "slippage_bps": D(20)})
    portfolio = PortfolioSnapshot(D(100000), D(100000), (), NOW, D(100000), account_id="synthetic")
    decision = strategy.decide(MARKET, portfolio, gapped_bars(gap_seconds))
    assert decision.rating == "REVIEW"
    assert decision.target_position_pct is None
    assert decision.actionable is False and decision.evaluation_eligible is False
    assert "consecutive 60-second" in decision.reason
    worker.assert_not_called()
