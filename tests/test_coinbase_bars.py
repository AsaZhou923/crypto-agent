"""Public candle isolation, schema validation and unchanged minute safety."""

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
import yaml

from crypto_agent.config import load_settings
from crypto_agent.data.coinbase import SOURCE, CoinbaseMinuteBars
from crypto_agent.data.features import build_intraday_features
from crypto_agent.models import AgentError, BrokerError, BrokerReadUnavailable, MarketSnapshot

NOW = datetime(2026, 10, 4, 3, 30, 25, tzinfo=UTC)


def candles():
    end = NOW.replace(second=0)
    return [
        [int((end - timedelta(minutes=i)).timestamp()), "99", "101", "100", "100", "1.23456789"]
        for i in range(62)
    ]


def provider(monkeypatch, payload=None, status=200):
    monkeypatch.setattr("crypto_agent.data.coinbase.utcnow", lambda: NOW)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=json.dumps(candles() if payload is None else payload))

    return CoinbaseMinuteBars(transport=httpx.MockTransport(handler)), calls


def features(bars, price=D(100)):
    market = MarketSnapshot(
        "BTC/USD", price, NOW, price - D(".01"), price + D(".01"), "alpaca-crypto-us-orderbook"
    )
    return build_intraday_features(
        market,
        bars,
        lookback_bars=60,
        min_bars=45,
        max_age_seconds=180,
        momentum_threshold_bps=D(2),
        now=NOW,
        expected_source=SOURCE,
        max_quote_bar_deviation_bps=D(100),
    )


def test_only_closed_actual_candles_with_explicit_window_and_no_credentials(monkeypatch):
    p, calls = provider(monkeypatch)
    try:
        bars = p.get_bars("BTC/USD", limit=60)
        assert len(bars) == 60 and features(bars).bar_count == 60
        assert bars[-1].observed_at == NOW.replace(second=0) - timedelta(minutes=1)
        assert bars[-1].volume == D("1.23456789") and bars[-1].source == SOURCE
        request = calls[0]
        assert request.url.host == "api.exchange.coinbase.com"
        assert request.url.path == "/products/BTC-USD/candles"
        assert request.url.params["granularity"] == "60"
        assert "start" in request.url.params and "end" in request.url.params
        assert not any("apca" in key or "authorization" in key for key in request.headers)
    finally:
        p.close()


@pytest.mark.parametrize(
    "fault", ["duplicate", "negative_volume", "crossed", "nonfinite", "bad_shape", "future", "off_minute"]
)
def test_invalid_provider_data_never_falls_back_or_fills(monkeypatch, fault):
    raw = candles()
    if fault == "duplicate":
        raw.append(raw[-1])
    elif fault == "negative_volume":
        raw[-1][-1] = "-1"
    elif fault == "crossed":
        raw[-1][1] = "102"
    elif fault == "nonfinite":
        raw[-1][4] = "NaN"
    elif fault == "bad_shape":
        raw[-1].pop()
    elif fault == "future":
        raw[-1][0] += 10000 * 60
    else:
        raw[-1][0] += 1
    p, _ = provider(monkeypatch, raw)
    try:
        with pytest.raises(BrokerError, match="missing or invalid"):
            p.get_bars("BTC/USD")
    finally:
        p.close()


def test_real_missing_minute_remains_rejected(monkeypatch):
    raw = candles()
    del raw[30]
    p, _ = provider(monkeypatch, raw)
    try:
        bars = p.get_bars("BTC/USD")
        with pytest.raises(AgentError, match="consecutive 60-second"):
            features(bars)
    finally:
        p.close()


def test_cross_venue_price_and_source_checks(monkeypatch):
    p, _ = provider(monkeypatch)
    try:
        bars = p.get_bars("BTC/USD")
        assert features(bars, D("100.99")).last_close == 100
        with pytest.raises(AgentError, match="diverges"):
            features(bars, D("101.01"))
        with pytest.raises(AgentError, match="configured source"):
            features(tuple(replace(b, source="other-provider") for b in bars))
    finally:
        p.close()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_public_data_failure_is_deferred(monkeypatch, status):
    p, calls = provider(monkeypatch, status=status)
    try:
        with pytest.raises(BrokerReadUnavailable):
            p.get_bars("BTC/USD")
        assert len(calls) == 1
    finally:
        p.close()


def test_source_and_price_bound_are_approval_bound_and_strict(tmp_path):
    for name in ("paper", "risk", "strategy"):
        shutil.copy2(Path("config/deployment/paper-session") / f"{name}.yaml", tmp_path / f"{name}.yaml")
    old = load_settings(tmp_path)
    path = tmp_path / "strategy.yaml"
    config = yaml.safe_load(path.read_text())
    config.update(intraday_bar_source="coinbase_exchange", intraday_max_quote_bar_deviation_bps=100)
    path.write_text(yaml.safe_dump(config))
    new = load_settings(tmp_path)
    assert new.digest != old.digest and new.paper == old.paper and new.risk == old.risk
    for invalid in (True, 0, 101, "100"):
        config["intraday_max_quote_bar_deviation_bps"] = invalid
        path.write_text(yaml.safe_dump(config))
        with pytest.raises(AgentError, match="intraday_max_quote_bar_deviation_bps"):
            load_settings(tmp_path)
