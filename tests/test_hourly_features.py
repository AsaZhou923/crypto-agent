"""Longer-horizon features still use literal contiguous 1-minute candles."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from crypto_agent.data.features import build_hourly_features
from crypto_agent.models import AgentError, MarketSnapshot, PriceBar

NOW = datetime(2026, 10, 7, 12, 0, 30, tzinfo=UTC)
MARKET = MarketSnapshot("BTC/USD", D(579), NOW, D("578.9"), D("579.1"), "synthetic-book")


def bars(count=480, *, end=NOW):
    start = end.replace(second=0, microsecond=0) - timedelta(minutes=count)
    return tuple(
        PriceBar(
            "BTC/USD",
            start + timedelta(minutes=index),
            D(100 + index),
            D(101 + index),
            D(99 + index),
            D(100 + index),
            D(1 + index % 7),
            "synthetic-minute-bars",
        )
        for index in range(count)
    )


def features(data, *, now=NOW, price=D(579)):
    return build_hourly_features(
        replace(MARKET, price=price),
        data,
        lookback_bars=480,
        min_bars=361,
        max_age_seconds=180,
        momentum_threshold_bps=D(2),
        now=now,
        expected_source="synthetic-minute-bars",
        max_quote_bar_deviation_bps=D(100),
    )


def test_hourly_features_use_named_one_three_six_hour_returns():
    result = features(bars())
    assert result.bar_count == 480
    assert result.first_bar_at == NOW.replace(second=0, microsecond=0) - timedelta(minutes=480)
    assert result.last_bar_at == NOW.replace(second=0, microsecond=0) - timedelta(minutes=1)
    assert result.return_60m_bps == (D(579) / D(519) - 1) * D(10000)
    assert result.return_180m_bps == (D(579) / D(399) - 1) * D(10000)
    assert result.return_360m_bps == (D(579) / D(219) - 1) * D(10000)
    assert result.ema_60_vs_180_bps > 0
    assert result.realized_vol_60m_bps > 0
    assert result.range_360m_bps > 0
    assert result.positive_move_proxy_bps == result.return_360m_bps
    assert result.return_evidence == (
        f"60m={result.return_60m_bps}",
        f"180m={result.return_180m_bps}",
        f"360m={result.return_360m_bps}",
    )
    assert result.payload()["positive_move_proxy_bps"] == result.positive_move_proxy_bps
    assert result.payload()["return_evidence"] == result.return_evidence


def test_hourly_features_reject_gaps_without_padding():
    data = bars()
    with pytest.raises(AgentError, match="consecutive 60-second"):
        features(
            tuple(
                replace(bar, observed_at=bar.observed_at - timedelta(minutes=1)) if index < 300 else bar
                for index, bar in enumerate(data)
            )
        )


def test_hourly_features_keep_literal_180_second_freshness_contract():
    assert features(bars(), now=NOW + timedelta(seconds=149)).bar_count == 480
    with pytest.raises(AgentError, match="stale"):
        features(bars(), now=NOW + timedelta(seconds=151))


def test_hourly_features_require_long_window_bounds():
    with pytest.raises(AgentError, match="Invalid intraday feature window"):
        build_hourly_features(
            MARKET,
            bars(360),
            lookback_bars=360,
            min_bars=360,
            max_age_seconds=180,
            momentum_threshold_bps=D(2),
            now=NOW,
        )
    with pytest.raises(AgentError, match="Invalid intraday feature window"):
        build_hourly_features(
            MARKET,
            bars(481),
            lookback_bars=481,
            min_bars=361,
            max_age_seconds=180,
            momentum_threshold_bps=D(2),
            now=NOW,
        )
    with pytest.raises(AgentError, match="Invalid intraday feature window"):
        build_hourly_features(
            MARKET,
            bars(599),
            lookback_bars=599,
            min_bars=361,
            max_age_seconds=180,
            momentum_threshold_bps=D(2),
            now=NOW,
        )
