"""Deterministic local broker accounting and restart tests; no external network."""

import sqlite3
from dataclasses import replace
from decimal import Decimal as D

import pytest

from crypto_agent.brokers.offline import OfflineBroker
from crypto_agent.models import BrokerError, OrderIntent, OrderRejected


def intent(client="buy-1", **changes):
    original = OrderIntent("BTC/USD", "buy", D("0.01"), client, D("50010"), D("500.10"), D("1.25025"))
    return replace(original, **changes)


def test_offline_buy_sell_uses_authoritative_fills_and_fees(tmp_path):
    broker = OfflineBroker(tmp_path / "broker.sqlite")
    assert "TEST DATA" in broker.get_market().source
    buy = broker.submit_order(intent())
    assert buy.status == "filled" and buy.filled_avg_price == D(50010)
    account = broker.get_portfolio()
    assert account.btc_quantity == D("0.01")
    assert account.cash_usd == D("9498.64975")
    sell = broker.submit_order(intent("sell-1", side="sell", limit_price=D(49990)))
    assert sell.status == "filled"
    account = broker.get_portfolio()
    assert account.btc_quantity == 0 and account.cash_usd == D("9997.30000")
    fills = [row for row in broker.get_activities() if row.kind == "FILL"]
    fees = [row for row in broker.get_activities() if row.kind == "FEE"]
    assert len(fills) == len(fees) == 2
    assert sum((row.fee_usd for row in fees), D(0)) == D("2.50000")


def test_duplicate_after_restart_does_not_fill_again(tmp_path):
    path = tmp_path / "broker.sqlite"
    broker = OfflineBroker(path)
    first = broker.submit_order(intent())
    account = broker.get_portfolio()
    broker.close()
    resumed = OfflineBroker(path)
    duplicate = resumed.submit_order(intent())
    assert duplicate == first
    assert resumed.get_portfolio().cash_usd == account.cash_usd
    assert resumed.get_portfolio().account_id == account.account_id
    assert len(resumed.get_activities()) == 2
    with pytest.raises(OrderRejected, match="different order"):
        resumed.submit_order(intent(quantity=D("0.02")))


def test_partial_restart_completion_and_cancel_release_reservations(tmp_path):
    path = tmp_path / "broker.sqlite"
    broker = OfflineBroker(path, fill_fraction=D("0.5"))
    order = broker.submit_order(intent())
    assert order.status == "partially_filled" and order.filled_quantity == D("0.005")
    assert len(broker.get_portfolio().open_orders) == 1
    broker.close()
    resumed = OfflineBroker(path, fill_fraction=D(0))
    assert resumed.get_order("buy-1").filled_quantity == D("0.005")
    order = resumed.fill_order("buy-1")
    assert order.status == "filled" and not resumed.get_portfolio().open_orders
    sell = resumed.submit_order(intent("sell-1", side="sell", limit_price=D("49990")))
    assert sell.status == "new" and resumed.get_portfolio().positions[0].available_quantity == 0
    resumed.cancel_order("sell-1")
    assert resumed.get_order("sell-1").status == "canceled"
    assert resumed.get_portfolio().positions[0].available_quantity == D("0.01")


def test_unmarketable_order_and_reserved_cash(tmp_path):
    broker = OfflineBroker(tmp_path / "broker.sqlite")
    order = broker.submit_order(intent(limit_price=D(49900)))
    assert order.status == "new" and broker.get_portfolio().btc_quantity == 0
    assert broker.get_portfolio().buying_power_usd < D(10000)
    broker.cancel_order(order.client_order_id)
    assert broker.get_portfolio().buying_power_usd == D(10000)


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": D("0.000001")},
        {"quantity": D("0.010000001")},
        {"limit_price": D("50010.001")},
        {"quantity": D(1)},
        {"side": "sell", "limit_price": D(49990)},
    ],
)
def test_no_subminimum_precision_overdraft_or_shorts(tmp_path, changes):
    broker = OfflineBroker(tmp_path / "broker.sqlite")
    with pytest.raises(OrderRejected):
        broker.submit_order(intent(**changes))
    assert broker.get_portfolio().cash_usd == 10000 and not broker.get_activities()


def test_ioc_remainder_canceled(tmp_path):
    broker = OfflineBroker(tmp_path / "broker.sqlite", fill_fraction=D("0.5"))
    order = broker.submit_order(intent(time_in_force="ioc"))
    assert order.status == "canceled" and order.filled_quantity == D("0.005")
    assert not broker.get_portfolio().open_orders


def test_state_contamination_and_fee_change_are_rejected(tmp_path):
    path = tmp_path / "broker.sqlite"
    OfflineBroker(path).close()
    with pytest.raises(BrokerError, match="fee differs"):
        OfflineBroker(path, fee_bps=D(10))
    other = tmp_path / "other.sqlite"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE runs (id TEXT)")
    with pytest.raises(BrokerError, match="mixed state"):
        OfflineBroker(other)
