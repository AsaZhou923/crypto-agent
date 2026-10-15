"""Public candle isolation, schema validation and unchanged minute safety."""

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

from crypto_agent.brokers.alpaca_paper import PAPER_URL, AlpacaPaperBroker
from crypto_agent.config import load_settings
from crypto_agent.data.coinbase import SOURCE, CoinbaseMinuteBars
from crypto_agent.data.features import build_intraday_features
from crypto_agent.models import AgentError, BrokerError, BrokerReadUnavailable, MarketSnapshot
from crypto_agent.risk.intraday import validate_entry_economics

NOW = datetime(2026, 10, 4, 3, 30, 25, tzinfo=UTC)


def candles():
    end = NOW.replace(second=0)
    return [
        [int((end - timedelta(minutes=i)).timestamp()), "99", "101", "100", "100", "1.23456789"]
        for i in range(62)
    ]


def candle_at(minute, *, close="100"):
    return [int(minute.timestamp()), "99", "101", "100", close, "1.23456789"]


def provider(monkeypatch, payload=None, status=200):
    monkeypatch.setattr("crypto_agent.data.coinbase.utcnow", lambda: NOW)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=json.dumps(candles() if payload is None else payload))

    return CoinbaseMinuteBars(transport=httpx.MockTransport(handler)), calls


def multipage_provider(monkeypatch, *, conflicting_boundary=False, unrelated_overlap=False):
    monkeypatch.setattr("crypto_agent.data.coinbase.utcnow", lambda: NOW)
    calls = []

    def handler(request):
        calls.append(request)
        params = parse_qs(request.url.query.decode())
        start = datetime.fromisoformat(params["start"][0])
        end = datetime.fromisoformat(params["end"][0])
        rows = []
        minute = start
        if unrelated_overlap and len(calls) == 2:
            rows.append(candle_at(start - timedelta(minutes=1)))
        while minute <= end:
            close = "100"
            if conflicting_boundary and len(calls) == 2 and minute == start:
                close = "100.5"
            rows.append(candle_at(minute, close=close))
            minute += timedelta(minutes=1)
        return httpx.Response(200, content=json.dumps(rows))

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


def test_long_coinbase_history_uses_bounded_pages_and_deduplicates_shared_boundary(monkeypatch):
    p, calls = multipage_provider(monkeypatch)
    try:
        bars = p.get_bars("BTC/USD", limit=481)
        assert len(calls) == 2
        assert len(bars) == 481
        assert bars[-1].observed_at == NOW.replace(second=0) - timedelta(minutes=1)
        for request in calls:
            params = parse_qs(request.url.query.decode())
            start = datetime.fromisoformat(params["start"][0])
            end = datetime.fromisoformat(params["end"][0])
            assert (end - start) <= timedelta(minutes=290)
        assert len({bar.observed_at for bar in bars}) == 481
    finally:
        p.close()


def test_final_page_past_shared_deadline_is_deferred_without_partial_bars(monkeypatch):
    ticks = iter([100, 100, 100, 100, 116])
    monkeypatch.setattr("crypto_agent.data.coinbase.monotonic", lambda: next(ticks))
    p, calls = multipage_provider(monkeypatch)
    try:
        with pytest.raises(BrokerReadUnavailable):
            p.get_bars("BTC/USD", limit=481)
        assert len(calls) == 2
    finally:
        p.close()


def test_conflicting_cross_page_duplicate_is_rejected(monkeypatch):
    p, _ = multipage_provider(monkeypatch, conflicting_boundary=True)
    try:
        with pytest.raises(BrokerError, match="missing or invalid"):
            p.get_bars("BTC/USD", limit=480)
    finally:
        p.close()


def test_unrelated_cross_page_overlap_is_rejected(monkeypatch):
    p, _ = multipage_provider(monkeypatch, unrelated_overlap=True)
    try:
        with pytest.raises(BrokerError, match="missing or invalid"):
            p.get_bars("BTC/USD", limit=480)
    finally:
        p.close()


def test_coinbase_history_limit_allows_request_padding_only_to_four_eighty_one(monkeypatch):
    p, _ = provider(monkeypatch)
    try:
        with pytest.raises(BrokerError, match="limit in \\[30, 481\\]"):
            p.get_bars("BTC/USD", limit=482)
    finally:
        p.close()


def test_alpaca_broker_delegates_extended_coinbase_history(monkeypatch):
    class Provider:
        def get_bars(self, symbol, *, timeframe, limit):
            assert symbol == "BTC/USD"
            assert timeframe == "1Min"
            assert limit == 480
            return ("delegated",)

        def close(self):
            pass

    monkeypatch.setattr("crypto_agent.data.coinbase.CoinbaseMinuteBars", lambda timeout: Provider())
    broker = AlpacaPaperBroker(
        PAPER_URL,
        "key",
        "secret",
        symbols=("BTC/USD",),
        bar_source="coinbase_exchange",
    )
    try:
        assert broker.get_bars("BTC/USD", limit=480) == ("delegated",)
    finally:
        broker.close()


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


def test_entry_execution_rechecks_source_and_cross_venue_price(monkeypatch):
    p, _ = provider(monkeypatch)
    try:
        bars = p.get_bars("BTC/USD")
        strategy = {
            "intraday_bar_source": "coinbase_exchange",
            "intraday_max_quote_bar_deviation_bps": 100,
            "intraday_require_cost_cover": True,
            "intraday_lookback_bars": 60,
            "intraday_min_bars": 45,
            "intraday_max_bar_age_seconds": 180,
            "intraday_momentum_threshold_bps": 2,
        }
        quote = MarketSnapshot("BTC/USD", D(102), NOW, D("101.99"), D("102.01"), "alpaca-crypto-us-orderbook")
        order = SimpleNamespace(side="buy")
        decision = SimpleNamespace(rating="Buy", created_at=NOW)
        risk = {"fee_buffer_bps": 30, "slippage_bps": 20}
        result = validate_entry_economics(order, quote, decision, bars, strategy, risk)
        assert not result.allowed and "diverges" in result.reasons[0]
        result = validate_entry_economics(
            order,
            replace(quote, price=D(100)),
            decision,
            tuple(replace(b, source="other") for b in bars),
            strategy,
            risk,
        )
        assert not result.allowed and "configured source" in result.reasons[0]
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
    path = tmp_path / "strategy.yaml"
    config = yaml.safe_load(path.read_text())
    config.pop("intraday_bar_source", None)
    config.pop("intraday_max_quote_bar_deviation_bps", None)
    config.update(decision_profile="intraday_10m", intraday_lookback_bars=60, intraday_min_bars=45)
    path.write_text(yaml.safe_dump(config))
    old = load_settings(tmp_path)
    config.update(intraday_bar_source="coinbase_exchange", intraday_max_quote_bar_deviation_bps=100)
    path.write_text(yaml.safe_dump(config))
    new = load_settings(tmp_path)
    assert new.digest != old.digest and new.paper == old.paper and new.risk == old.risk
    for invalid in (True, 0, 101, "100"):
        config["intraday_max_quote_bar_deviation_bps"] = invalid
        path.write_text(yaml.safe_dump(config))
        with pytest.raises(AgentError, match="intraday_max_quote_bar_deviation_bps"):
            load_settings(tmp_path)
