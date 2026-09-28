"""Write-ahead submission. An ambiguous request is queried, never retransmitted."""

from collections.abc import Callable

from crypto_agent.brokers.base import Broker
from crypto_agent.models import (
    TERMINAL_STATUSES,
    AgentError,
    BrokerError,
    BrokerOrder,
    OrderIntent,
    OrderRejected,
    PortfolioSnapshot,
    RiskResult,
    SubmissionUnknown,
    timestamp,
)
from crypto_agent.storage.database import Database, intent_from_json


def _match(order: OrderIntent, result: BrokerOrder) -> None:
    if (
        result.client_order_id != order.client_order_id
        or result.symbol != order.symbol
        or result.side != order.side
        or result.quantity != order.quantity
        or result.limit_price != order.limit_price
    ):
        raise BrokerError(
            "Broker order identity differs from persisted intent; manual reconciliation required"
        )


def _save(order: OrderIntent, result: BrokerOrder, database: Database) -> BrokerOrder:
    _match(order, result)
    database.order_state(order.client_order_id, result.status, result)
    return result


def execute_order(
    order: OrderIntent,
    broker: Broker,
    database: Database,
    *,
    before_submit: Callable[[PortfolioSnapshot], RiskResult] | None = None,
) -> dict:
    """Caller holds database lock and has freshly validated the persisted preview."""
    portfolio = verify_broker_identity(broker, database)
    row = next((row for row in database.orders() if row["client_order_id"] == order.client_order_id), None)
    if row is None or intent_from_json(row["intent"]) != order:
        raise AgentError("Execution requires an unchanged persisted order preview")
    if row["attempted"]:
        return reconcile_order(row, broker, database)
    # Also protects against a previously accepted order already known remotely.
    existing = broker.get_order(order.client_order_id)
    if existing is not None:
        database.claim_submission(order.client_order_id)
        return {"order": _save(order, existing, database), "submitted": False}
    # Reads may retry long enough to expire the validated quote or decision.
    # Recheck only after all preflight HTTP, while the intent is still unclaimed.
    if before_submit is not None:
        risk = before_submit(portfolio)
        if not risk.allowed:
            return {"status": "blocked", "risk": risk, "submitted": False}
    if not database.claim_submission(order.client_order_id):
        raise AgentError("Preview already claimed; reconcile before proceeding")
    try:
        result = broker.submit_order(order)
    except OrderRejected:
        database.order_state(order.client_order_id, "rejected")
        return {"status": "rejected", "client_order_id": order.client_order_id, "submitted": False}
    except (SubmissionUnknown, BrokerError, TimeoutError, OSError):
        database.order_state(order.client_order_id, "unknown")
        return _query_after_unknown(order, broker, database)
    except Exception:
        # Commit uncertainty even when an adapter raises an unexpected error.
        database.order_state(order.client_order_id, "unknown")
        raise AgentError("Submission outcome unknown; run reconcile before further trading") from None
    try:
        _save(order, result, database)
    except BrokerError:
        database.order_state(order.client_order_id, "unknown")
        raise
    return {"order": result, "submitted": True}


def _query_after_unknown(order, broker, database) -> dict:
    try:
        result = broker.get_order(order.client_order_id)
        if result is not None:
            return {
                "order": _save(order, result, database),
                "submitted": False,
                "recovered_after_uncertainty": True,
            }
    except (BrokerError, TimeoutError, OSError):
        pass
    return {
        "status": "unknown",
        "client_order_id": order.client_order_id,
        "message": "No safe retry: request may have been accepted. Reconcile by client_order_id.",
    }


def reconcile_order(row: dict, broker: Broker, database: Database) -> dict:
    intent = intent_from_json(row["intent"])
    result = broker.get_order(intent.client_order_id)
    if result is None:
        if row["status"] in TERMINAL_STATUSES:
            return {
                "client_order_id": intent.client_order_id,
                "status": row["status"],
                "broker_lookup": "not_found",
            }
        database.order_state(intent.client_order_id, "unknown")
        return {"client_order_id": intent.client_order_id, "status": "unknown"}
    return {"order": _save(intent, result, database), "submitted": False}


def reconcile(broker: Broker, database: Database, *, refresh_terminal: bool = False) -> dict:
    verify_broker_identity(broker, database)
    tracked = database.orders(attempted_only=True)
    # Terminal outcomes have already been confirmed and persisted. Re-reading
    # every historical order on every account check grows HTTP traffic forever.
    # Pending/unknown orders are ALWAYS refreshed; explicit audits can refresh all.
    selected = [row for row in tracked if refresh_terminal or row["status"] not in TERMINAL_STATUSES]
    orders = [reconcile_order(row, broker, database) for row in selected]
    started = database.metadata("tracking_started_at")
    activities = broker.get_activities(after=timestamp(started)) if started else []
    database.activities(activities)
    markets = broker.get_markets(tuple(broker.symbols))
    portfolio = broker.get_portfolio()
    database.snapshot(portfolio, markets, establish_baseline=False)
    return {
        "mode": broker.mode,
        "orders": orders,
        "terminal_orders_skipped": len(tracked) - len(selected),
        "activity_records_seen": len(activities),
        "portfolio": portfolio,
        "market": markets[broker.symbols[0]],
        "markets": markets,
    }


def verify_broker_identity(broker: Broker, database: Database):
    """Check account/environment before any ledger mutation or external write."""
    database.bind("mode", broker.mode)
    portfolio = broker.get_portfolio()
    database.bind("account_id", portfolio.account_id)
    return portfolio
