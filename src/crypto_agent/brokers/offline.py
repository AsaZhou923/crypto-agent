"""Persistent, deterministic TEST DATA broker; never contacts Alpaca.

Synthetic fills execute at the fixture bid/ask and charge an explicit USD fee.
This intentionally simplified simulator is for workflow verification only.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from uuid import uuid4

from crypto_agent.brokers.alpaca_paper import parse_order
from crypto_agent.models import (
    SYMBOL,
    TERMINAL_STATUSES,
    Activity,
    AssetRules,
    BrokerError,
    BrokerOrder,
    MarketSnapshot,
    OrderIntent,
    OrderRejected,
    PortfolioSnapshot,
    Position,
    PriceBar,
    decimal,
    dumps,
    normalize_symbol,
    timestamp,
    utcnow,
)


class OfflineBroker:
    mode = "offline"
    symbols = (SYMBOL,)
    RULES = AssetRules(SYMBOL, Decimal("0.0001"), Decimal("0.00000001"), Decimal("0.01"))

    def __init__(
        self,
        path: Path,
        fee_bps: Decimal = Decimal(25),
        *,
        fill_fraction: Decimal = Decimal(1),
    ):
        self.fee_bps = decimal(fee_bps)
        self.fill_fraction = decimal(fill_fraction)
        if not 0 <= self.fee_bps <= 1000 or not 0 <= self.fill_fraction <= 1:
            raise BrokerError("Invalid offline fee or fill fraction")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=15, isolation_level=None)
        try:
            tables = {
                row[0]
                for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and tables != {"offline_state"}:
                raise BrokerError("Offline broker requires its own state database; refusing mixed state")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS offline_state (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL)"
            )
            with self._transaction() as state:
                if not state:
                    state.update(
                        {
                            "mode": "offline",
                            "version": 1,
                            "account_id": "offline-test-" + uuid4().hex,
                            "fee_bps": str(self.fee_bps),
                            "cash": "10000",
                            "quantity": "0",
                            "average_entry_price": "0",
                            "orders": {},
                            "activities": [],
                        }
                    )
                if state.get("mode") != "offline" or state.get("version") != 1:
                    raise BrokerError("Offline state mode or version mismatch")
                if decimal(state["fee_bps"]) != self.fee_bps:
                    raise BrokerError("Offline state fee differs from configuration; use a fresh state file")
        except (sqlite3.Error, ValueError, KeyError):
            self._connection.close()
            raise BrokerError("Offline state database is corrupt or incompatible") from None
        except BrokerError:
            self._connection.close()
            raise

    @contextmanager
    def _transaction(self):
        # Serializes independent processes and persists orders, cash and fills atomically.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute("SELECT state FROM offline_state WHERE id=1").fetchone()
            state = json.loads(row[0]) if row else {}
            yield state
            self._connection.execute("INSERT OR REPLACE INTO offline_state VALUES (1, ?)", (dumps(state),))
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def _read(self):
        row = self._connection.execute("SELECT state FROM offline_state WHERE id=1").fetchone()
        if row is None:
            raise BrokerError("Offline state is missing")
        return json.loads(row[0])

    def get_market(self, symbol: str = SYMBOL) -> MarketSnapshot:
        if normalize_symbol(symbol) != SYMBOL:
            raise BrokerError("Offline demo provides synthetic BTC/USD data only")
        return MarketSnapshot(
            SYMBOL,
            Decimal(50000),
            utcnow(),
            Decimal(49990),
            Decimal(50010),
            "OFFLINE SYNTHETIC TEST DATA (fixed price, refreshed simulation clock)",
        )

    def get_markets(self, symbols: tuple[str, ...]) -> dict[str, MarketSnapshot]:
        if tuple(symbols) != (SYMBOL,):
            raise BrokerError("Offline demo provides synthetic BTC/USD data only")
        return {SYMBOL: self.get_market()}

    def get_bars(
        self, symbol: str = SYMBOL, *, timeframe: str = "1Min", limit: int = 60
    ) -> tuple[PriceBar, ...]:
        """Clearly labelled deterministic bars for local workflow tests only."""
        if normalize_symbol(symbol) != SYMBOL or timeframe != "1Min" or not 30 <= limit <= 240:
            raise BrokerError("Offline bars support BTC/USD 1Min with limit in [30, 240]")
        minute = utcnow().replace(second=0, microsecond=0) - timedelta(minutes=limit)
        bars = []
        for index in range(limit):
            close = Decimal(50000) + Decimal(index % 12 - 6)
            opened = close - Decimal("0.5")
            bars.append(
                PriceBar(
                    SYMBOL,
                    minute + timedelta(minutes=index),
                    opened,
                    close + Decimal(1),
                    opened - Decimal(1),
                    close,
                    Decimal(1),
                    "OFFLINE SYNTHETIC TEST DATA (1-minute bars)",
                )
            )
        return tuple(bars)

    def get_asset_rules(self, symbol: str = SYMBOL) -> AssetRules:
        if normalize_symbol(symbol) != SYMBOL:
            raise BrokerError("Offline demo provides synthetic BTC/USD rules only")
        return self.RULES

    def _open_orders(self, state) -> tuple[BrokerOrder, ...]:
        return tuple(
            parse_order(item) for item in state["orders"].values() if item["status"] not in TERMINAL_STATUSES
        )

    def get_portfolio(self) -> PortfolioSnapshot:
        state = self._read()
        cash, quantity = decimal(state["cash"]), decimal(state["quantity"])
        orders = self._open_orders(state)
        reserved_cash = sum(
            (
                order.remaining_quantity * order.limit_price * (1 + self.fee_bps / 10000)
                for order in orders
                if order.side == "buy"
            ),
            Decimal(0),
        )
        reserved_quantity = sum(
            (order.remaining_quantity for order in orders if order.side == "sell"), Decimal(0)
        )
        positions = (
            (
                Position(
                    SYMBOL,
                    quantity,
                    decimal(state["average_entry_price"]),
                    quantity - reserved_quantity,
                ),
            )
            if quantity
            else ()
        )
        return PortfolioSnapshot(
            cash,
            cash + quantity * self.get_market().price,
            positions,
            utcnow(),
            max(Decimal(0), cash - reserved_cash),
            orders,
            True,
            state["account_id"],
        )

    def submit_order(self, order: OrderIntent) -> BrokerOrder:
        normalize_symbol(order.symbol)
        rules = self.RULES
        if (
            order.side not in {"buy", "sell"}
            or order.time_in_force not in {"gtc", "ioc"}
            or not order.quantity.is_finite()
            or order.quantity < rules.min_order_size
            or order.quantity % rules.quantity_increment != 0
            or not order.limit_price.is_finite()
            or order.limit_price <= 0
            or order.limit_price % rules.price_increment != 0
            or not order.client_order_id
            or len(order.client_order_id) > 48
        ):
            raise OrderRejected("Offline order violates supported spot order precision or limits")
        with self._transaction() as state:
            previous = state["orders"].get(order.client_order_id)
            if previous:
                existing = parse_order(previous)
                if (existing.side, existing.quantity, existing.limit_price) != (
                    order.side,
                    order.quantity,
                    order.limit_price,
                ):
                    raise OrderRejected("Client order ID already belongs to a different order")
                return existing
            open_orders = self._open_orders(state)
            if order.side == "buy":
                reserved = sum(
                    (
                        item.remaining_quantity * item.limit_price * (1 + self.fee_bps / 10000)
                        for item in open_orders
                        if item.side == "buy"
                    ),
                    Decimal(0),
                )
                cost = order.quantity * order.limit_price * (1 + self.fee_bps / 10000)
                if cost > decimal(state["cash"]) - reserved:
                    raise OrderRejected("Offline insufficient cash; borrowing is prohibited")
            else:
                reserved = sum(
                    (item.remaining_quantity for item in open_orders if item.side == "sell"), Decimal(0)
                )
                if order.quantity > decimal(state["quantity"]) - reserved:
                    raise OrderRejected("Offline insufficient BTC; short selling is prohibited")
            now = utcnow().isoformat()
            item = {
                "id": "offline-" + uuid4().hex,
                "client_order_id": order.client_order_id,
                "symbol": SYMBOL,
                "side": order.side,
                "qty": str(order.quantity),
                "filled_qty": "0",
                "filled_avg_price": None,
                "status": "new",
                "created_at": now,
                "updated_at": now,
                "limit_price": str(order.limit_price),
            }
            state["orders"][order.client_order_id] = item
            if self.fill_fraction > 0:
                self._fill(state, item, self.fill_fraction)
            if order.time_in_force == "ioc" and item["status"] != "filled":
                item["status"] = "canceled"
                item["updated_at"] = utcnow().isoformat()
            return parse_order(item)

    def _fill(self, state, item, fraction):
        order = parse_order(item)
        if order.status in TERMINAL_STATUSES:
            return
        market = self.get_market()
        price = market.ask if order.side == "buy" else market.bid
        if (order.side == "buy" and price > order.limit_price) or (
            order.side == "sell" and price < order.limit_price
        ):
            return
        increment = self.RULES.quantity_increment
        quantity = (order.remaining_quantity * fraction / increment).to_integral_value(
            rounding=ROUND_DOWN
        ) * increment
        if quantity <= 0:
            return
        fee = quantity * price * self.fee_bps / 10000
        old_qty, cash = decimal(state["quantity"]), decimal(state["cash"])
        if order.side == "buy":
            new_qty = old_qty + quantity
            state["cash"] = str(cash - quantity * price - fee)
            state["average_entry_price"] = str(
                (old_qty * decimal(state["average_entry_price"]) + quantity * price) / new_qty
            )
        else:
            new_qty = old_qty - quantity
            state["cash"] = str(cash + quantity * price - fee)
            if new_qty == 0:
                state["average_entry_price"] = "0"
        state["quantity"] = str(new_qty)
        total_filled = order.filled_quantity + quantity
        total_value = order.filled_quantity * (order.filled_avg_price or 0) + quantity * price
        now = utcnow()
        item.update(
            {
                "filled_qty": str(total_filled),
                "filled_avg_price": str(total_value / total_filled),
                "status": "filled" if total_filled == order.quantity else "partially_filled",
                "updated_at": now.isoformat(),
            }
        )
        for activity in (
            Activity(uuid4().hex, "FILL", now, SYMBOL, order.broker_order_id, order.side, quantity, price),
            Activity(uuid4().hex, "FEE", now, SYMBOL, order.broker_order_id, fee_usd=fee),
        ):
            state["activities"].append(json.loads(dumps(activity)))

    def fill_order(self, client_order_id: str, fraction: Decimal = Decimal(1)) -> BrokerOrder:
        """Test helper: fill this fraction of the remaining quantity at the fixture quote."""
        fraction = decimal(fraction)
        if not 0 < fraction <= 1:
            raise BrokerError("Offline fill fraction must be in (0, 1]")
        with self._transaction() as state:
            if client_order_id not in state["orders"]:
                raise BrokerError("Offline order not found")
            item = state["orders"][client_order_id]
            self._fill(state, item, fraction)
            return parse_order(item)

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        item = self._read()["orders"].get(client_order_id)
        return parse_order(item) if item else None

    def cancel_order(self, client_order_id: str) -> None:
        with self._transaction() as state:
            item = state["orders"].get(client_order_id)
            if item is None:
                raise BrokerError("Offline order not found")
            if item["status"] not in TERMINAL_STATUSES:
                item["status"] = "canceled"
                item["updated_at"] = utcnow().isoformat()

    def get_activities(self, after: datetime | None = None) -> list[Activity]:
        results = []
        for item in self._read()["activities"]:
            occurred = timestamp(item["occurred_at"])
            if after is not None and occurred <= timestamp(after):
                continue
            results.append(
                Activity(
                    item["activity_id"],
                    item["kind"],
                    occurred,
                    item["symbol"],
                    item["order_id"],
                    item["side"],
                    decimal(item["quantity"]),
                    decimal(item["price"]),
                    decimal(item["fee_usd"]) if item["fee_usd"] is not None else None,
                    item.get("time_precision", "instant"),
                )
            )
        return results

    def close(self) -> None:
        self._connection.close()
