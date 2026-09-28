"""Adversarial checks at the independent risk boundary."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from crypto_agent.config import RISK_NUMBERS, load_settings
from crypto_agent.execution.planner import plan_orders
from crypto_agent.models import AssetRules, MarketSnapshot, PortfolioSnapshot, Position, TradeDecision, utcnow
from crypto_agent.risk.checks import validate_inputs, validate_order


@pytest.fixture
def sample():
    now = utcnow()
    risk = load_settings(Path("config/demo"), "offline").risk
    market = MarketSnapshot("BTC/USD", D(50000), now, D(49990), D(50010), "test-fixture")
    account = PortfolioSnapshot(D(10000), D(10000), (), now, D(10000))
    asset = AssetRules("BTC/USD", D("0.0001"), D("0.00000001"), D("0.01"))
    decision = TradeDecision(
        "BTC/USD",
        D("0.1"),
        "Synthetic threshold test",
        now + timedelta(seconds=240),
        now,
        "Buy",
        "test-v1",
        "none",
        ("Synthetic fixture quote",),
    )
    return decision, market, account, asset, risk, now


def check(sample, **changes):
    decision, market, portfolio, asset, risk, now = sample
    args = dict(
        decision=decision,
        market=market,
        portfolio=portfolio,
        asset=asset,
        risk=risk,
        daily_start_equity=D(10000),
        now=now,
    )
    args.update(changes)
    return validate_inputs(**args)


def test_complete_valid_inputs_pass(sample):
    assert check(sample).allowed


def test_per_symbol_price_bound_cannot_be_relaxed_by_global_range(sample):
    low = replace(sample[1], price=D(50), bid=D(49), ask=D(51))
    risk = {**sample[4], "min_price_usd": D(".001")}
    result = check(sample, market=low, risk=risk)
    assert not result.allowed and "sanity" in result.reasons[0]


@pytest.mark.parametrize(
    "missing",
    [
        *RISK_NUMBERS,
        "allowed_symbols",
        "price_bounds_usd",
        "allow_short",
        "allow_leverage",
        "require_order_preview",
    ],
)
def test_each_missing_risk_limit_blocks(sample, missing):
    risk = dict(sample[4])
    del risk[missing]
    result = check(sample, risk=risk)
    assert not result.allowed
    assert "Missing required" in result.reasons[0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("allow_short", True),
        ("allow_leverage", True),
        ("require_order_preview", False),
        ("allowed_symbols", ["BTC/USD", "ETH/USD", "SOL/USD", "BTC/USD"]),
        ("max_position_pct", D("NaN")),
        ("max_daily_loss_usd", 0),
    ],
)
def test_unsafe_limits_block(sample, field, value):
    assert not check(sample, risk={**sample[4], field: value}).allowed


@pytest.mark.parametrize(
    "field,offset", [("market", -61), ("market", 6), ("portfolio", -61), ("portfolio", 6)]
)
def test_stale_and_future_market_and_account_block(sample, field, offset):
    original = sample[1] if field == "market" else sample[2]
    changed = replace(original, observed_at=sample[5] + timedelta(seconds=offset))
    assert not check(sample, **{field: changed}).allowed


@pytest.mark.parametrize(
    "updates",
    [
        {"rating": "REVIEW"},
        {"rating": "Buy because momentum"},
        {"actionable": False},
        {"target_position_pct": None},
        {"target_position_pct": D("NaN")},
        {"target_position_pct": D("0.21")},
        {"evidence": ()},
        {"evidence": ("",)},
        {"evidence": "unstructured"},
        {"reason": " "},
        {"symbol": "BTCUSD"},
        {"model": ""},
    ],
)
def test_malformed_or_nonactionable_decision_blocks(sample, updates):
    assert not check(sample, decision=replace(sample[0], **updates)).allowed


def test_expired_and_overlong_decision_block(sample):
    now = sample[5]
    for update in (
        {"expires_at": now},
        {"created_at": now - timedelta(seconds=301), "expires_at": now + timedelta(seconds=1)},
        {"created_at": now + timedelta(seconds=6)},
        {"expires_at": now + timedelta(seconds=301)},
    ):
        assert not check(sample, decision=replace(sample[0], **update)).allowed


@pytest.mark.parametrize(
    "updates",
    [
        {"price": D(0)},
        {"price": D("NaN")},
        {"ask": D("Infinity")},
        {"bid": D(50020)},
        {"ask": D(51000)},
        {"symbol": "ETH/USD"},
    ],
)
def test_bad_market_blocks(sample, updates):
    assert not check(sample, market=replace(sample[1], **updates)).allowed


@pytest.mark.parametrize(
    "updates",
    [
        {"tradable": False},
        {"cash_usd": D(-1)},
        {"equity_usd": D(0)},
        {"buying_power_usd": D(-1)},
    ],
)
def test_bad_account_blocks(sample, updates):
    assert not check(sample, portfolio=replace(sample[2], **updates)).allowed


@pytest.mark.parametrize(
    "updates",
    [
        {"tradable": False},
        {"quantity_increment": D(0)},
        {"price_increment": D(0)},
        {"min_order_size": D(0)},
        {"symbol": "BTCUSD"},
    ],
)
def test_untradable_or_invalid_asset_blocks(sample, updates):
    assert not check(sample, asset=replace(sample[3], **updates)).allowed


def test_daily_loss_at_boundary_blocks_even_sell(sample):
    account = replace(
        sample[2], equity_usd=D(9900), positions=(Position("BTC/USD", D(".01"), D(50000), D(".01")),)
    )
    decision = replace(sample[0], target_position_pct=D(0), rating="Sell")
    result = check(sample, decision=decision, portfolio=account)
    assert not result.allowed
    assert "daily equity loss" in result.reasons[0]


def test_existing_excess_allows_only_reduction(sample):
    account = replace(sample[2], positions=(Position("BTC/USD", D(".05"), D(50000), D(".05")),))
    assert check(
        sample, decision=None, portfolio=account
    ).allowed  # allow observing before choosing a reduction
    assert check(sample, portfolio=account).allowed  # target 10%, current 25%
    decision = replace(sample[0], rating="Underweight")
    orders = plan_orders(decision, sample[1], account, sample[3], sample[4], "reduce")
    assert orders[0].side == "sell"
    assert validate_order(orders[0], decision, sample[1], account, sample[3], sample[4], D(10000)).allowed


def test_order_recomputes_notional_instead_of_trusting_estimates(sample):
    decision, market, account, asset, risk, _ = sample
    intent = plan_orders(decision, market, account, asset, risk, "test")[0]
    intent = replace(intent, quantity=D(".04"), estimated_notional_usd=D(1), estimated_fee_usd=D(0))
    result = validate_order(intent, decision, market, account, asset, risk, D(10000))
    assert not result.allowed
    assert "notional" in result.reasons[0]


@pytest.mark.parametrize(
    "update,reason",
    [
        ({"quantity": D(".010000001")}, "precision"),
        ({"limit_price": D("50110.021")}, "precision"),
        ({"limit_price": D(51000)}, "slippage"),
        ({"side": "short"}, "side"),
        ({"time_in_force": "day"}, "gtc"),
        ({"client_order_id": ""}, "client_order_id"),
    ],
)
def test_tampered_orders_fail_independent_risk(sample, update, reason):
    decision, market, account, asset, risk, _ = sample
    intent = replace(plan_orders(decision, market, account, asset, risk, "test")[0], **update)
    result = validate_order(intent, decision, market, account, asset, risk, D(10000))
    assert not result.allowed
    assert reason in result.reasons[0]


def test_cash_and_maximum_position_rechecked_after_planning(sample):
    decision, market, account, asset, risk, _ = sample
    intent = plan_orders(decision, market, account, asset, risk, "test")[0]
    low_cash = replace(account, cash_usd=D(1))
    result = validate_order(intent, decision, market, low_cash, asset, risk, D(10000))
    assert not result.allowed and "Insufficient" in result.reasons[0]
    capped = {**risk, "max_position_pct": D(".1")}
    # Raw target quantity ignores fee/slippage; independently reject it.
    intent = replace(intent, quantity=D(".02"))
    result = validate_order(intent, decision, market, account, asset, capped, D(10000))
    assert not result.allowed and "Projected position" in result.reasons[0]


@pytest.mark.parametrize("rating,target", [("Buy", "0.01"), ("Overweight", "0.01"), ("Underweight", "0.1")])
def test_quote_refresh_cannot_reverse_rating_side(sample, rating, target):
    decision, market, portfolio, asset, risk, now = sample
    portfolio = replace(portfolio, positions=(Position("BTC/USD", D(".01"), D(49000), D(".01")),))
    decision = replace(decision, rating=rating, target_position_pct=D(target))
    assert plan_orders(decision, market, portfolio, asset, risk, "direction-test") == []


@pytest.mark.parametrize(
    "rating,side", [("Buy", "sell"), ("Overweight", "sell"), ("Underweight", "buy"), ("Sell", "buy")]
)
def test_independent_risk_rejects_opposite_rating_side(sample, rating, side):
    decision, market, portfolio, asset, risk, now = sample
    order = plan_orders(decision, market, portfolio, asset, risk, "direction-test")[0]
    result = validate_order(
        replace(order, side=side),
        replace(decision, rating=rating),
        market,
        portfolio,
        asset,
        risk,
        D(10000),
        now=now,
    )
    assert not result.allowed and "side conflicts" in result.reasons[0]


@pytest.mark.parametrize("quantity", [D(".001"), D(".0099")])
def test_absolute_position_cap_uses_limit_price_even_when_equity_grows(sample, quantity):
    decision, market, account, asset, risk, now = sample
    risk = {**risk, "cap_order_to_limit": True, "max_order_notional_usd": D(500)}
    account = replace(
        account,
        equity_usd=D(100000),
        positions=(Position("BTC/USD", D(".099"), D(40000), D(".099")),),
    )
    (order,) = plan_orders(decision, market, account, asset, risk, "absolute-cap")
    order = replace(order, quantity=quantity, estimated_notional_usd=D(1), estimated_fee_usd=D(0))
    assert validate_order(order, decision, market, account, asset, risk, account.equity_usd, now).allowed
    capped = {**risk, "max_position_notional_usd": D(5000)}
    result = validate_order(order, decision, market, account, asset, capped, account.equity_usd, now)
    assert not result.allowed
    assert result.reasons == ("Projected position exceeds maximum USD notional",)
    if quantity == D(".001"):
        assert (account.quantity_for("BTC/USD") + quantity) * market.price == D(5000)


def test_absolute_total_cap_uses_current_other_coin_quotes_and_survives_equity_growth(sample):
    decision, market, account, asset, risk, now = sample
    risk = {
        **risk,
        "allowed_symbols": ["BTC/USD", "ETH/USD"],
        "price_bounds_usd": {**risk["price_bounds_usd"], "ETH/USD": {"min": D(100), "max": D(100000)}},
        "cap_order_to_limit": True,
        "max_order_notional_usd": D(500),
        "max_position_notional_usd": D(5000),
    }
    account = replace(account, equity_usd=D(100000), positions=(Position("ETH/USD", D(2), D(1000), D(2)),))
    quotes = {"ETH/USD": D(4800)}
    (order,) = plan_orders(decision, market, account, asset, risk, "total-cap", market_prices=quotes)
    assert validate_order(
        order, decision, market, account, asset, risk, account.equity_usd, now, market_prices=quotes
    ).allowed
    capped = {**risk, "max_total_position_notional_usd": D(10000)}
    result = validate_order(
        order, decision, market, account, asset, capped, account.equity_usd, now, market_prices=quotes
    )
    assert not result.allowed
    assert result.reasons == ("Projected aggregate portfolio exceeds maximum USD notional",)
    assert validate_order(
        order,
        decision,
        market,
        account,
        asset,
        capped,
        account.equity_usd,
        now,
        market_prices={"ETH/USD": D(4700)},
    ).allowed


def test_sell_reduces_appreciated_positions_even_when_both_absolute_caps_remain_exceeded(sample):
    decision, market, account, asset, risk, now = sample
    risk = {
        **risk,
        "allowed_symbols": ["BTC/USD", "ETH/USD"],
        "price_bounds_usd": {**risk["price_bounds_usd"], "ETH/USD": {"min": D(100), "max": D(100000)}},
        "cap_order_to_limit": True,
        "max_order_notional_usd": D(500),
        "max_position_notional_usd": D(5000),
        "max_total_position_notional_usd": D(10000),
    }
    decision = replace(decision, rating="Sell", target_position_pct=D(0))
    account = replace(
        account,
        equity_usd=D(100000),
        positions=(
            Position("BTC/USD", D(".12"), D(40000), D(".12")),
            Position("ETH/USD", D(2), D(1000), D(2)),
        ),
    )
    quotes = {"ETH/USD": D(3000)}
    (order,) = plan_orders(decision, market, account, asset, risk, "reduce-usd-excess", market_prices=quotes)
    remaining = (account.quantity_for("BTC/USD") - order.quantity) * market.price
    assert remaining > risk["max_position_notional_usd"]
    assert remaining + D(2) * quotes["ETH/USD"] > risk["max_total_position_notional_usd"]
    assert validate_order(
        order, decision, market, account, asset, risk, account.equity_usd, now, market_prices=quotes
    ).allowed
