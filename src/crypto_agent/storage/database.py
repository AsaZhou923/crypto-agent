"""Durable runs, separate decision/risk/order/fill ledgers, and observed account state."""

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from crypto_agent.models import (
    Activity,
    AgentError,
    BrokerOrder,
    MarketSnapshot,
    OrderIntent,
    PortfolioSnapshot,
    TradeDecision,
    decimal,
    dumps,
    timestamp,
    utcnow,
)


class Database:
    def __init__(self, path: Path, mode: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL,
                status TEXT NOT NULL, config_digest TEXT NOT NULL, config_json TEXT NOT NULL,
                market_json TEXT, portfolio_json TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS decisions (
                run_id TEXT PRIMARY KEY REFERENCES runs(id), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intraday_contexts (
                run_id TEXT PRIMARY KEY REFERENCES runs(id), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS risk_results (
                id INTEGER PRIMARY KEY, run_id TEXT REFERENCES runs(id),
                phase TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL REFERENCES runs(id),
                intent TEXT NOT NULL, status TEXT NOT NULL, attempted INTEGER NOT NULL DEFAULT 0,
                broker_json TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills (
                activity_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, order_id TEXT,
                body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fees (
                activity_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL,
                portfolio_json TEXT NOT NULL, market_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL,
                symbol TEXT NOT NULL, market_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_baselines (
                day TEXT PRIMARY KEY, equity TEXT NOT NULL, observed_at TEXT NOT NULL);
        """)
        try:
            self.bind("mode", mode)
        except AgentError:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    @contextmanager
    def lock(self):
        """Cross-process single writer, held through planning/submission/reconciliation."""
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AgentError("Another process is using this trading database") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def bind(self, key: str, value: str):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, value))
            existing = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]
        if existing != value:
            raise AgentError(f"Database {key} mismatch; use a separate database for each account/environment")

    def metadata(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def snapshot(
        self,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot | dict[str, MarketSnapshot],
        establish_baseline: bool = True,
    ) -> Decimal | None:
        markets = market if isinstance(market, dict) else {market.symbol: market}
        if not markets:
            raise AgentError("At least one market snapshot is required")
        primary = next(iter(markets.values()))
        self.bind("account_id", portfolio.account_id)
        observed = timestamp(portfolio.observed_at).isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('tracking_started_at',?)", (observed,)
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('opening_portfolio',?)", (dumps(portfolio),)
            )
            if establish_baseline:
                self.connection.execute(
                    "INSERT OR IGNORE INTO daily_baselines VALUES (?,?,?)",
                    (observed[:10], str(portfolio.equity_usd), observed),
                )
            self.connection.execute(
                "INSERT INTO account_snapshots(observed_at,portfolio_json,market_json) VALUES (?,?,?)",
                (observed, dumps(portfolio), dumps(primary)),
            )
            for symbol, item in markets.items():
                if item.symbol != symbol:
                    raise AgentError("Market snapshot key/symbol mismatch")
                self.connection.execute(
                    "INSERT INTO market_snapshots(observed_at,symbol,market_json) VALUES (?,?,?)",
                    (timestamp(item.observed_at).isoformat(), symbol, dumps(item)),
                )
        return self.daily_baseline(portfolio.observed_at) if establish_baseline else None

    def daily_baseline(self, now: datetime) -> Decimal:
        day = timestamp(now).date().isoformat()
        row = self.connection.execute("SELECT equity FROM daily_baselines WHERE day=?", (day,)).fetchone()
        if not row:
            raise AgentError("Missing observed UTC daily equity baseline")
        return decimal(row[0])

    def create_run(self, run_id: str, mode: str, config_digest: str, summary: dict):
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs(id,created_at,mode,status,config_digest,config_json) VALUES (?,?,?,'started',?,?)",
                (run_id, utcnow().isoformat(), mode, config_digest, dumps(summary)),
            )

    def run_inputs(self, run_id: str, market, portfolio):
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET market_json=?,portfolio_json=? WHERE id=?",
                (dumps(market), dumps(portfolio), run_id),
            )

    def decision(self, run_id: str, decision: TradeDecision):
        with self.connection:
            self.connection.execute("INSERT INTO decisions VALUES (?,?)", (run_id, dumps(decision)))

    def intraday_context(self, run_id: str, bars):
        with self.connection:
            self.connection.execute(
                "INSERT INTO intraday_contexts VALUES (?,?)", (run_id, dumps(tuple(bars)))
            )

    def risk(self, run_id: str, phase: str, result):
        with self.connection:
            self.connection.execute(
                "INSERT INTO risk_results(run_id,phase,created_at,body) VALUES (?,?,?,?)",
                (run_id, phase, utcnow().isoformat(), dumps(result)),
            )

    def finish(self, run_id: str, status: str, error: str | None = None):
        with self.connection:
            self.connection.execute("UPDATE runs SET status=?,error=? WHERE id=?", (status, error, run_id))

    def preview(self, run_id: str, order: OrderIntent):
        with self.connection:
            self.connection.execute(
                "INSERT INTO orders(client_order_id,run_id,intent,status,updated_at) VALUES (?,?,?,'preview',?)",
                (order.client_order_id, run_id, dumps(order), utcnow().isoformat()),
            )
            self.connection.execute("UPDATE runs SET status='preview' WHERE id=?", (run_id,))

    def get_run(self, run_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise AgentError("Run not found")
        result = dict(row)
        decision = self.connection.execute("SELECT body FROM decisions WHERE run_id=?", (run_id,)).fetchone()
        result["decision"] = decision_from_json(decision[0]) if decision else None
        return result

    def order_for_run(self, run_id: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM orders WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def orders(self, attempted_only: bool = False) -> list[dict]:
        where = " WHERE attempted=1" if attempted_only else ""
        rows = self.connection.execute(
            "SELECT * FROM orders" + where + " ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def claim_submission(self, client_order_id: str) -> bool:
        # Commit before HTTP request. Crash before/after POST leaves attempted=1,
        # which can ONLY be queried; never automatically resubmitted.
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE orders SET attempted=1,status='submitting',updated_at=? WHERE client_order_id=? AND attempted=0 AND status='preview'",
                (utcnow().isoformat(), client_order_id),
            )
        return cursor.rowcount == 1

    def order_state(self, client_order_id: str, status: str, broker_order: BrokerOrder | None = None):
        with self.connection:
            self.connection.execute(
                "UPDATE orders SET status=?,broker_json=COALESCE(?,broker_json),updated_at=? WHERE client_order_id=?",
                (
                    status,
                    dumps(broker_order) if broker_order else None,
                    utcnow().isoformat(),
                    client_order_id,
                ),
            )
            self.connection.execute(
                "UPDATE runs SET status=? WHERE id=(SELECT run_id FROM orders WHERE client_order_id=?)",
                (status, client_order_id),
            )

    def activities(self, activities: list[Activity]):
        with self.connection:
            for activity in activities:
                if activity.kind == "FILL":
                    self.connection.execute(
                        "INSERT OR IGNORE INTO fills VALUES (?,?,?,?)",
                        (
                            activity.activity_id,
                            timestamp(activity.occurred_at).isoformat(),
                            activity.order_id,
                            dumps(activity),
                        ),
                    )
                elif activity.kind in {"CFEE", "FEE"}:
                    body = dumps(activity)
                    existing = self.connection.execute(
                        "SELECT body FROM fees WHERE activity_id=?", (activity.activity_id,)
                    ).fetchone()
                    if not existing:
                        self.connection.execute(
                            "INSERT INTO fees VALUES (?,?,?)",
                            (activity.activity_id, timestamp(activity.occurred_at).isoformat(), body),
                        )
                    else:
                        merged = _merge_fee_activity(json.loads(existing["body"]), json.loads(body))
                        if merged is not None and merged != json.loads(existing["body"]):
                            self.connection.execute(
                                "UPDATE fees SET occurred_at=?, body=? WHERE activity_id=?",
                                (
                                    timestamp(activity.occurred_at).isoformat(),
                                    dumps(merged),
                                    activity.activity_id,
                                ),
                            )
                else:
                    raise AgentError("Unsupported broker activity type")

    def recent_runs(self, limit: int = 20) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT id,created_at,mode,status,config_digest,error FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def latest_snapshot(self, symbol: str | None = None) -> dict | None:
        row = self.connection.execute("SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        market = None
        if row and symbol is not None:
            market_row = self.connection.execute(
                "SELECT market_json FROM market_snapshots WHERE symbol=? ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            market = json.loads(market_row[0]) if market_row else None
        return (
            {
                "portfolio": json.loads(row["portfolio_json"]),
                "market": market or json.loads(row["market_json"]),
            }
            if row
            else None
        )

    def report(self) -> dict:
        last = self.latest_snapshot()
        if not last:
            return {"mode": self.metadata("mode"), "status": "no_observed_account_data"}
        opening = json.loads(self.metadata("opening_portfolio"))
        quantities = {p["symbol"]: decimal(p["quantity"]) for p in opening["positions"]}
        costs = {
            p["symbol"]: decimal(p["quantity"]) * decimal(p["average_entry_price"])
            for p in opening["positions"]
        }
        gross_realized = Decimal(0)
        complete = True
        fee_attribution_uncertain = False
        records = [
            json.loads(row[0])
            for row in self.connection.execute("SELECT body FROM fills UNION ALL SELECT body FROM fees")
        ]

        def event_order(record):
            # Fee dates describe a day, not an intraday execution timestamp.
            occurred = record["occurred_at"]
            if record.get("time_precision") == "day":
                occurred = occurred[:10] + "T23:59:59.999999+00:00"
            return occurred, record["kind"] != "FILL", record["activity_id"]

        records.sort(key=event_order)
        fee_total = Decimal(0)
        unknown_fees = 0
        fill_count = 0
        for activity in records:
            qty, price = decimal(activity["quantity"]), decimal(activity["price"])
            if activity["kind"] == "FILL":
                fill_count += 1
                symbol = activity["symbol"]
                quantity = quantities.get(symbol, Decimal(0))
                cost = costs.get(symbol, Decimal(0))
                if activity["side"] == "buy":
                    quantity += qty
                    cost += qty * price
                elif activity["side"] == "sell":
                    if qty > quantity or quantity <= 0:
                        complete = False
                    else:
                        average = cost / quantity
                        gross_realized += qty * (price - average)
                        cost -= qty * average
                        quantity -= qty
                quantities[symbol], costs[symbol] = quantity, cost
            else:
                if activity.get("time_precision") == "day" and not activity.get("order_id"):
                    fee_attribution_uncertain = True
                if activity["fee_usd"] is None:
                    unknown_fees += 1
                else:
                    fee_total += decimal(activity["fee_usd"])
                if activity["kind"] == "CFEE" and activity["symbol"] and qty:
                    symbol = activity["symbol"]
                    quantity = quantities.get(symbol, Decimal(0))
                    cost = costs.get(symbol, Decimal(0))
                    if qty < 0:
                        reduction = abs(qty)
                        if quantity >= reduction and quantity > 0:
                            cost -= reduction * cost / quantity
                            quantity -= reduction
                        else:
                            complete = False
                        quantities[symbol], costs[symbol] = quantity, cost
                    elif qty > 0:
                        complete = False
                        quantities[symbol], costs[symbol] = quantity, cost
                    else:
                        complete = False
                        quantities[symbol], costs[symbol] = quantity, cost
        p = last["portfolio"]
        latest_markets = {}
        for row in self.connection.execute(
            "SELECT m.symbol,m.market_json FROM market_snapshots m "
            "JOIN (SELECT symbol,max(id) id FROM market_snapshots GROUP BY symbol) x ON x.id=m.id"
        ):
            latest_markets[row["symbol"]] = json.loads(row["market_json"])
        unrealized = Decimal(0)
        for pos in p["positions"]:
            if pos["symbol"] not in latest_markets:
                complete = False
                continue
            mark = decimal(latest_markets[pos["symbol"]]["price"])
            unrealized += decimal(pos["quantity"]) * (mark - decimal(pos["average_entry_price"]))
        observed_quantities = {pos["symbol"]: decimal(pos["quantity"]) for pos in p["positions"]}
        quantity_differences = {}
        for symbol in set(observed_quantities) | set(quantities):
            difference = quantities.get(symbol, Decimal(0)) - observed_quantities.get(symbol, Decimal(0))
            if difference:
                quantity_differences[symbol] = str(difference)
            if abs(difference) > Decimal("0.00000001"):
                complete = False
        paper = self.metadata("mode") == "paper"
        pending_fee_notice = (
            "Paper fee attribution is provisional: same-day asset-fee entries may post or enrich later, "
            "but unresolved position differences are not treated as proven pending fees."
            if paper
            else None
        )
        return {
            "mode": self.metadata("mode"),
            "as_of": p["observed_at"],
            "market_observed_at": last["market"]["observed_at"],
            "market_observed_at_by_symbol": {
                symbol: value["observed_at"] for symbol, value in latest_markets.items()
            },
            "tracking_started_at": self.metadata("tracking_started_at"),
            "equity_usd": decimal(p["equity_usd"]),
            "observed_equity_change_usd": decimal(p["equity_usd"]) - decimal(opening["equity_usd"]),
            "realized_pnl_gross_usd": gross_realized if complete else None,
            "unrealized_pnl_usd": unrealized,
            "recorded_fees_usd": fee_total,
            "unvalued_fee_records": unknown_fees,
            "position_quantity_differences": quantity_differences,
            "realized_minus_recorded_fees_usd": gross_realized - fee_total
            if complete and not unknown_fees and not fee_attribution_uncertain
            else None,
            "fill_count": fill_count,
            "ledger_matches_position": complete,
            "fees_may_be_pending": paper,
            "pending_fee_notice": pending_fee_notice,
            "fee_attribution_uncertain": fee_attribution_uncertain,
            "realized_basis_provisional": fee_attribution_uncertain,
            "basis": "Weighted average cost from opening broker position and subsequent broker FILL activities; unrealized uses latest observed broker position cost and quote midpoint.",
            "limitations": "Paper fees may post later. Equity change is not cash-flow adjusted. External deposits, withdrawals, transfers or missing activities invalidate performance attribution. Reconcile before reporting.",
            "notice": "Synthetic offline / paper simulation only; no real-money return or profitability guarantee.",
        }


def decision_from_json(body: str) -> TradeDecision:
    value = json.loads(body)
    value["target_position_pct"] = (
        decimal(value["target_position_pct"]) if value["target_position_pct"] is not None else None
    )
    value["created_at"], value["expires_at"] = timestamp(value["created_at"]), timestamp(value["expires_at"])
    value["evidence"] = tuple(value["evidence"])
    return TradeDecision(**value)


def intent_from_json(body: str) -> OrderIntent:
    value = json.loads(body)
    for key in ("quantity", "limit_price", "estimated_notional_usd", "estimated_fee_usd"):
        value[key] = decimal(value[key])
    return OrderIntent(**value)


def _merge_fee_activity(current: dict, incoming: dict) -> dict | None:
    if current.get("kind") != incoming.get("kind"):
        return None
    for key in ("activity_id", "occurred_at", "time_precision"):
        if current.get(key) not in (None, "", 0, "0") and incoming.get(key) not in (None, "", 0, "0"):
            if current[key] != incoming[key]:
                return None
    merged = dict(current)
    for key in ("fee_usd", "symbol", "currency", "side", "order_id"):
        if current.get(key) in (None, "") and incoming.get(key) not in (None, ""):
            merged[key] = incoming[key]
        elif current.get(key) not in (None, "") and incoming.get(key) not in (None, ""):
            if current[key] != incoming[key]:
                return None
    for key in ("quantity", "price"):
        current_value = decimal(current.get(key, 0))
        incoming_value = decimal(incoming.get(key, 0))
        if current_value == 0 and incoming_value != 0:
            merged[key] = incoming[key]
        elif current_value != 0 and incoming_value != 0 and current_value != incoming_value:
            return None
    return merged


def initialize_database(path: Path) -> None:
    Database(path, "paper").close()
