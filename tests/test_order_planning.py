"""Order sizing, pending exposure and exchange precision regressions."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from crypto_agent.config import load_settings
from crypto_agent.execution.planner import plan_orders
from crypto_agent.models import (
    AgentError,
    AssetRules,
    BrokerOrder,
    MarketSnapshot,
    PortfolioSnapshot,
    Position,
    TradeDecision,
    utcnow,
)
from crypto_agent.risk.checks import validate_order


@pytest.fixture
def sample():
    now = utcnow()
    market = MarketSnapshot("BTC/USD", D(50000), now, D(49990), D(50010), "test-fixture")
    account = PortfolioSnapshot(D(10000), D(10000), (), now, D(10000))
    asset = AssetRules("BTC/USD", D(".0001"), D(".00000001"), D(".01"))
    risk = load_settings(Path("config/demo"), "offline").risk
    decision = TradeDecision(
        "BTC/USD",
        D(".1"),
        "Fixture target",
        now + timedelta(seconds=240),
        now,
        "Buy",
        "test-v1",
        "none",
        ("Test evidence",),
    )
    return decision, market, account, asset, risk


def test_buy_reserves_slippage_fees_and_exchange_precision(sample):
    decision, market, account, asset, risk = sample
    (order,) = plan_orders(*sample, "one")
    assert order.side == "buy"
    assert D(0) < order.quantity < D(".02")
    assert order.quantity % asset.quantity_increment == 0
    assert order.limit_price == D("50110.02")
    assert order.estimated_fee_usd == order.quantity * order.limit_price * D(".003")
    assert order.estimated_notional_usd / (account.equity_usd - order.estimated_fee_usd) <= D(".1")
    assert validate_order(order, decision, market, account, asset, risk, account.equity_usd).allowed


def test_at_target_and_subminimum_dust_are_noops(sample):
    decision, market, account, asset, risk = sample
    for quantity in (D(".02"), D(".01999999")):
        held = replace(account, positions=(Position("BTC/USD", quantity, D(50000), quantity),))
        assert plan_orders(decision, market, held, asset, risk, "noop") == []


def test_existing_holdings_reduce_buy_size(sample):
    decision, market, account, asset, risk = sample
    held = replace(account, positions=(Position("BTC/USD", D(".01"), D(50000), D(".01")),))
    (order,) = plan_orders(decision, market, held, asset, risk, "existing")
    assert D(0) < order.quantity < D(".01")
    assert validate_order(order, decision, market, held, asset, risk, account.equity_usd).allowed


def test_partial_fill_counts_only_pending_remainder(sample):
    decision, market, account, asset, risk = sample
    pending = BrokerOrder(
        "broker-id",
        "pending-id",
        "BTC/USD",
        "buy",
        D(".02"),
        D(".01"),
        D(50000),
        "partially_filled",
        utcnow(),
    )
    held = replace(
        account, positions=(Position("BTC/USD", D(".01"), D(50000), D(".01")),), open_orders=(pending,)
    )
    assert plan_orders(decision, market, held, asset, risk, "no-repeat") == []


@pytest.mark.parametrize("side,quantity", [("buy", D(".01")), ("buy", D(".03")), ("sell", D(".005"))])
def test_unresolved_pending_orders_block_replanning(sample, side, quantity):
    decision, market, account, asset, risk = sample
    pending = BrokerOrder("broker-id", "pending-id", "BTC/USD", side, quantity, D(0), None, "new", utcnow())
    pending_account = replace(account, open_orders=(pending,))
    with pytest.raises(AgentError, match="Reconcile/cancel"):
        plan_orders(decision, market, pending_account, asset, risk, "next")
    (order,) = plan_orders(*sample, "one")
    result = validate_order(order, decision, market, pending_account, asset, risk, account.equity_usd)
    assert not result.allowed and "pending" in result.reasons[0]


def test_buy_caps_to_available_nonmargin_cash(sample):
    decision, market, account, asset, risk = sample
    limited = replace(account, cash_usd=D(900), buying_power_usd=D(125))
    (order,) = plan_orders(decision, market, limited, asset, risk, "small")
    cost = order.estimated_notional_usd + order.estimated_fee_usd
    assert D(124) < cost <= D(125)
    assert validate_order(order, decision, market, limited, asset, risk, account.equity_usd).allowed
    assert plan_orders(decision, market, replace(limited, cash_usd=D(0)), asset, risk, "empty") == []


def test_sell_only_available_quantity_and_floor_limit(sample):
    decision, market, account, asset, risk = sample
    held = replace(account, positions=(Position("BTC/USD", D(".02"), D(48000), D(".005123456789")),))
    sell = replace(decision, target_position_pct=D(0), rating="Sell")
    (order,) = plan_orders(sell, market, held, asset, risk, "sell")
    assert order.side == "sell"
    assert order.quantity == D(".00512345")
    assert order.limit_price == D("49890.02")
    assert validate_order(order, sell, market, held, asset, risk, account.equity_usd).allowed
    too_much = replace(order, quantity=D(".006"))
    rejected = validate_order(too_much, sell, market, held, asset, risk, account.equity_usd)
    assert not rejected.allowed and "available" in rejected.reasons[0]


@pytest.mark.parametrize("cap", [None, False])
def test_order_cap_is_rejected_without_silent_splitting(sample, cap):
    decision, market, account, asset, risk = sample
    if cap is not None:
        risk = {**risk, "cap_order_to_limit": cap}
    target = replace(decision, target_position_pct=D(".2"))
    (order,) = plan_orders(target, market, account, asset, risk, "too-large")
    assert order.estimated_notional_usd > risk["max_order_notional_usd"]
    result = validate_order(order, target, market, account, asset, risk, account.equity_usd)
    assert not result.allowed and "maximum" in result.reasons[0]


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_opt_in_cap_returns_one_order_at_limit_price_with_recomputed_fees(sample, side):
    decision, market, account, asset, risk = sample
    risk = {**risk, "cap_order_to_limit": True, "max_order_notional_usd": D(500)}
    if side == "sell":
        decision = replace(decision, target_position_pct=D(0), rating="Sell")
        account = replace(account, positions=(Position("BTC/USD", D(".04"), D(50000), D(".04")),))
    orders = plan_orders(decision, market, account, asset, risk, "persistent-id")
    assert len(orders) == 1
    order = orders[0]
    assert order.side == side
    assert order.client_order_id == "persistent-id"
    assert order.quantity % asset.quantity_increment == 0
    assert order.estimated_notional_usd == order.quantity * order.limit_price
    assert order.estimated_notional_usd <= D(500)
    assert (order.quantity + asset.quantity_increment) * order.limit_price > D(500)
    assert order.estimated_fee_usd == order.estimated_notional_usd * D(".003")
    assert validate_order(order, decision, market, account, asset, risk, account.equity_usd).allowed


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_opt_in_cap_preserves_tighter_cash_or_available_bounds(sample, side):
    decision, market, account, asset, risk = sample
    risk = {**risk, "cap_order_to_limit": True, "max_order_notional_usd": D(500)}
    if side == "buy":
        account = replace(account, cash_usd=D(100), buying_power_usd=D(100))
    else:
        decision = replace(decision, target_position_pct=D(0), rating="Sell")
        account = replace(account, positions=(Position("BTC/USD", D(".04"), D(50000), D(".002")),))
    (order,) = plan_orders(decision, market, account, asset, risk, "bounded")
    assert order.estimated_notional_usd < D(100)
    assert validate_order(order, decision, market, account, asset, risk, account.equity_usd).allowed


@pytest.mark.parametrize(
    "minimum,increment,min_notional",
    [(D(".0001"), D(".01"), D(1)), (D(".01"), D(".0001"), D(1)), (D(".0001"), D(".001"), D(499))],
)
def test_opt_in_cap_does_not_round_up_to_meet_exchange_minimum(sample, minimum, increment, min_notional):
    decision, market, account, asset, risk = sample
    risk = {
        **risk,
        "cap_order_to_limit": True,
        "max_order_notional_usd": D(500),
        "min_order_notional_usd": min_notional,
    }
    asset = replace(asset, min_order_size=minimum, quantity_increment=increment)
    assert plan_orders(decision, market, account, asset, risk, "dust") == []


def test_fresh_decision_cycles_respect_position_limit_cash_and_cumulative_fees(sample):
    decision, market, account, asset, risk = sample
    risk = {**risk, "cap_order_to_limit": True, "max_order_notional_usd": D(500)}
    target = replace(decision, target_position_pct=risk["max_position_pct"])
    starting_cash = account.cash_usd
    spent = fees = held = D(0)
    orders = []
    for cycle in range(10):
        now = utcnow()
        fresh_decision = replace(target, created_at=now, expires_at=now + timedelta(seconds=240))
        fresh_market = replace(market, observed_at=now)
        account = replace(account, observed_at=now)
        planned = plan_orders(fresh_decision, fresh_market, account, asset, risk, f"cycle-{cycle}")
        if not planned:
            break
        (order,) = planned
        assert validate_order(
            order, fresh_decision, fresh_market, account, asset, risk, starting_cash
        ).allowed
        assert order.estimated_notional_usd <= D(500)
        orders.append(order)
        spent += order.estimated_notional_usd
        fees += order.estimated_fee_usd
        held += order.quantity
        cash = starting_cash - spent - fees
        equity = cash + held * market.price
        account = replace(
            account,
            cash_usd=cash,
            buying_power_usd=cash,
            equity_usd=equity,
            positions=(Position("BTC/USD", held, spent / held, held),),
        )
        assert cash >= 0
        assert held * market.price / equity <= risk["max_position_pct"]
    else:
        pytest.fail("Capped orders did not converge to a subminimum target remainder")
    assert len(orders) > 1
    assert fees == sum(order.estimated_notional_usd for order in orders) * D(".003")
    assert account.cash_usd == starting_cash - spent - fees
    assert orders[-1].estimated_notional_usd < D(500)


def test_opt_in_cap_preserves_noop_when_sell_price_rounds_to_zero(sample):
    decision, market, account, asset, risk = sample
    risk = {**risk, "cap_order_to_limit": True, "max_order_notional_usd": D(500)}
    decision = replace(decision, target_position_pct=D(0), rating="Sell")
    account = replace(account, positions=(Position("BTC/USD", D(".04"), D(50000), D(".04")),))
    asset = replace(asset, price_increment=D(100000))
    assert plan_orders(decision, market, account, asset, risk, "zero-price") == []


@pytest.mark.parametrize("rating", ["Buy", "Overweight", "Sell", "Underweight", "Hold"])
def test_opt_in_cap_preserves_hold_and_direction_gates(sample, rating):
    decision, market, account, asset, risk = sample
    risk = {**risk, "cap_order_to_limit": True, "max_order_notional_usd": D(500)}
    target = D(".1")
    if rating in {"Buy", "Overweight", "Hold"}:
        account = replace(account, positions=(Position("BTC/USD", D(".04"), D(50000), D(".04")),))
    decision = replace(decision, rating=rating, target_position_pct=target, actionable=rating != "Hold")
    assert plan_orders(decision, market, account, asset, risk, "no-reversal") == []


def test_max_position_target_remains_under_limit_after_reserves(sample):
    decision, market, account, asset, risk = sample
    capped = {**risk, "max_position_pct": D(".1")}
    (order,) = plan_orders(decision, market, account, asset, capped, "cap")
    assert validate_order(order, decision, market, account, asset, capped, account.equity_usd).allowed


def test_unactionable_or_expired_decision_never_creates_order(sample):
    decision, market, account, asset, risk = sample
    for update in ({"rating": "REVIEW"}, {"expires_at": utcnow() - timedelta(seconds=1)}, {"evidence": ()}):
        with pytest.raises(AgentError):
            plan_orders(replace(decision, **update), market, account, asset, risk, "blocked")


def test_valid_hold_is_noop_even_if_account_changes_after_analysis(sample):
    decision, market, account, asset, risk = sample
    hold = replace(decision, rating="Hold", actionable=False, target_position_pct=D(0))
    changed = replace(account, positions=(Position("BTC/USD", D(".05"), D(50000), D(".05")),))
    assert plan_orders(hold, market, changed, asset, risk, "hold") == []


def test_pending_sells_cannot_offset_excess_committed_buys(sample):
    decision, market, account, asset, risk = sample
    buy = BrokerOrder("buy", "buy-client", "BTC/USD", "buy", D(".05"), D(0), None, "new", utcnow())
    sell = BrokerOrder("sell", "sell-client", "BTC/USD", "sell", D(".05"), D(0), None, "new", utcnow())
    pending = replace(account, open_orders=(buy, sell))
    with pytest.raises(AgentError, match="exceeds limit"):
        plan_orders(decision, market, pending, asset, risk, "gross")
