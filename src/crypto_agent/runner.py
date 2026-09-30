"""Manual observe -> decide -> risk -> preview -> explicit execute -> reconcile."""

import json
import os
from dataclasses import replace
from datetime import timedelta
from time import monotonic, sleep
from uuid import uuid4

from crypto_agent.config import Settings, require_model
from crypto_agent.execution.executor import execute_order, reconcile, verify_broker_identity
from crypto_agent.execution.planner import plan_orders
from crypto_agent.models import (
    TERMINAL_STATUSES,
    AgentError,
    PriceBar,
    RiskResult,
    decimal,
    dumps,
    normalize_symbol,
    timestamp,
    utcnow,
)
from crypto_agent.risk.checks import validate_inputs, validate_order
from crypto_agent.risk.intraday import validate_entry_economics, validate_intraday_order
from crypto_agent.storage.database import Database, intent_from_json
from crypto_agent.strategies.entry_gate import begin_attempt, filter_decision, validate_entry_preview


def make_broker(settings: Settings, allow_submit: bool = False):
    if settings.mode == "offline":
        from crypto_agent.brokers.offline import OfflineBroker

        return OfflineBroker(settings.database_path.with_suffix(".broker.sqlite"))
    from crypto_agent.brokers.alpaca_paper import AlpacaPaperBroker

    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise AgentError("Missing local ALPACA_API_KEY / ALPACA_SECRET_KEY; configure .env locally")
    return AlpacaPaperBroker(
        base_url=settings.paper["base_url"],
        api_key=key,
        secret_key=secret,
        timeout_seconds=settings.paper["request_timeout_seconds"],
        allow_submit=allow_submit,
        symbols=tuple(settings.paper["symbols"]),
    )


def make_strategy(settings: Settings):
    if settings.strategy["name"] == "baseline":
        from crypto_agent.strategies.baseline import BaselineStrategy

        return BaselineStrategy(settings.strategy)
    if settings.mode == "offline":
        raise AgentError("Offline mode requires baseline strategy; AI uses external services")
    require_model(settings)
    if settings.strategy["name"] == "intraday_ai":
        from crypto_agent.strategies.intraday_ai import IntradayAIStrategy

        return IntradayAIStrategy(settings.strategy, settings.risk)
    from crypto_agent.strategies.tradingagents import TradingAgentsStrategy

    return TradingAgentsStrategy(settings.strategy)


def _unresolved(database: Database):
    return [row for row in database.orders(attempted_only=True) if row["status"] not in TERMINAL_STATUSES]


def _account_signature(portfolio) -> str:
    return dumps(
        {
            "account": portfolio.account_id,
            "cash": portfolio.cash_usd,
            "positions": portfolio.positions,
            "orders": portfolio.open_orders,
        }
    )


def _markets(
    broker,
    symbols: list[str],
    selected_symbol: str | None = None,
    max_age_seconds=None,
    wait_seconds: int = 0,
) -> dict:
    deadline = monotonic() + wait_seconds
    while True:
        if hasattr(broker, "get_markets"):
            markets = broker.get_markets(tuple(symbols))
        elif len(symbols) == 1:
            market = broker.get_market()
            markets = {market.symbol: market}
        else:
            raise AgentError("Broker adapter cannot provide all configured market prices")
        if selected_symbol is None or max_age_seconds is None:
            return markets
        selected = markets[selected_symbol]
        age = (utcnow() - timestamp(selected.observed_at)).total_seconds()
        if -5 <= age <= float(max_age_seconds):
            return markets
        remaining = deadline - monotonic()
        if age < -5 or remaining <= 0:
            return markets
        sleep(min(5, remaining))


def _intraday_bars_current(bars, decision, settings, now=None):
    now = now or utcnow()
    ends = [
        timestamp(bar["observed_at"] if isinstance(bar, dict) else bar.observed_at) + timedelta(minutes=1)
        for bar in bars
    ]
    closed = [end for end in ends if end <= timestamp(decision.created_at)]
    return bool(closed) and 0 <= (now - max(closed)).total_seconds() <= float(
        settings.strategy.get("intraday_max_bar_age_seconds", 180)
    )


def _bars_from_json(value):
    try:
        raw = json.loads(value)
        if not isinstance(raw, list):
            raise ValueError
        return tuple(
            PriceBar(
                **{
                    **bar,
                    "observed_at": timestamp(bar["observed_at"]),
                    **{key: decimal(bar[key], key) for key in ("open", "high", "low", "close", "volume")},
                }
            )
            for bar in raw
        )
    except (AgentError, TypeError, KeyError, ValueError, json.JSONDecodeError):
        raise AgentError("Invalid stored intraday context") from None


def _validate_entry_economics_from_context(order, market, decision, context, strategy, risk):
    try:
        bars = _bars_from_json(context[0]) if context else ()
    except AgentError as exc:
        return RiskResult(False, (str(exc),))
    return validate_entry_economics(order, market, decision, bars, strategy, risk)


def run_once(
    settings: Settings,
    broker,
    database: Database,
    analysis_only: bool = False,
    strategy=None,
    symbol: str | None = None,
    cycle_started_at=None,
) -> dict:
    with database.lock():
        if broker.mode != settings.mode:
            raise AgentError("Broker mode does not match configuration")
        run_id = uuid4().hex
        database.create_run(run_id, settings.mode, settings.digest, settings.summary)
        try:
            symbol = normalize_symbol(symbol or settings.paper["symbols"][0])
            if symbol not in settings.paper["symbols"]:
                raise AgentError("Selected symbol is outside the configured universe")
            begin_attempt(database, settings, run_id, symbol, cycle_started_at=cycle_started_at)
            verify_broker_identity(broker, database)
            if _unresolved(database):
                raise AgentError("Outstanding or uncertain recorded order: reconcile/cancel before a new run")
            markets = _markets(
                broker,
                settings.paper["symbols"],
                symbol,
                settings.risk["max_data_age_seconds"],
                settings.paper["fresh_market_wait_seconds"],
            )
            market, portfolio, asset = markets[symbol], broker.get_portfolio(), broker.get_asset_rules(symbol)
            prices = {key: value.price for key, value in markets.items()}
            initial = validate_inputs(
                None, market, portfolio, asset, settings.risk, portfolio.equity_usd, market_prices=prices
            )
            database.run_inputs(run_id, market, portfolio)
            database.risk(run_id, "input", initial)
            if not initial.allowed:
                database.finish(run_id, "blocked")
                return {"run_id": run_id, "status": "blocked", "risk": initial}
            daily = database.snapshot(portfolio, markets)
            initial = validate_inputs(
                None, market, portfolio, asset, settings.risk, daily, market_prices=prices
            )
            database.risk(run_id, "daily_loss", initial)
            if not initial.allowed:
                database.finish(run_id, "blocked")
                return {"run_id": run_id, "status": "blocked", "risk": initial}
            selected_strategy = strategy or make_strategy(settings)
            if getattr(selected_strategy, "requires_intraday_bars", False) is True:
                request = selected_strategy.bar_request
                bars = broker.get_bars(symbol, **request)
                database.intraday_context(run_id, bars)
                decision = selected_strategy.decide(market, portfolio, bars)
                if not _intraday_bars_current(bars, decision, settings):
                    decision = replace(
                        decision,
                        rating="REVIEW",
                        target_position_pct=None,
                        actionable=False,
                        evaluation_eligible=False,
                        reason="Intraday minute bars expired during analysis",
                    )
            else:
                decision = selected_strategy.decide(market, portfolio)
            decision = filter_decision(
                database, settings, run_id, decision, market_observed_at=market.observed_at
            )
            database.decision(run_id, decision)
            if decision.rating == "REVIEW" or decision.target_position_pct is None:
                from crypto_agent.models import RiskResult

                result = RiskResult(False, (decision.reason,))
                database.risk(run_id, "decision", result)
                database.finish(run_id, "no_order")
                return {
                    "run_id": run_id,
                    "status": "no_order",
                    "decision": decision,
                    "risk": result,
                    "message": "REVIEW is a normal no-order outcome",
                }
            # AI can take minutes. Fetch fresh execution data; do not extend decision TTL.
            fresh_markets, fresh_portfolio = (
                _markets(
                    broker,
                    settings.paper["symbols"],
                    symbol,
                    settings.risk["max_data_age_seconds"],
                    settings.paper["fresh_market_wait_seconds"],
                ),
                broker.get_portfolio(),
            )
            fresh_market = fresh_markets[symbol]
            fresh_prices = {key: value.price for key, value in fresh_markets.items()}
            if _account_signature(portfolio) != _account_signature(fresh_portfolio):
                raise AgentError("Account changed during analysis; create a fresh decision")
            if fresh_portfolio.observed_at.date() != portfolio.observed_at.date():
                raise AgentError("Analysis crossed UTC date; create a fresh run")
            database.snapshot(fresh_portfolio, fresh_markets, establish_baseline=False)
            result = validate_inputs(
                decision,
                fresh_market,
                fresh_portfolio,
                asset,
                settings.risk,
                daily,
                market_prices=fresh_prices,
            )
            database.risk(run_id, "decision", result)
            if not result.allowed:
                database.finish(run_id, "blocked")
                return {"run_id": run_id, "status": "blocked", "decision": decision, "risk": result}
            if analysis_only:
                database.finish(run_id, "analyzed")
                return {"run_id": run_id, "status": "analyzed", "decision": decision, "risk": result}
            orders = plan_orders(
                decision,
                fresh_market,
                fresh_portfolio,
                asset,
                settings.risk,
                "ca-" + run_id,
                market_prices=fresh_prices,
            )
            if not orders:
                database.finish(run_id, "no_order")
                return {
                    "run_id": run_id,
                    "status": "no_order",
                    "decision": decision,
                    "message": "Target already satisfied, non-actionable Hold, or remainder below minimum size",
                }
            order = orders[0]
            result = validate_order(
                order,
                decision,
                fresh_market,
                fresh_portfolio,
                asset,
                settings.risk,
                daily,
                market_prices=fresh_prices,
            )
            if result.allowed:
                result = validate_intraday_order(
                    order, fresh_market, fresh_portfolio, settings.strategy, settings.risk
                )
            if result.allowed and "intraday_require_cost_cover" in settings.strategy:
                context = database.connection.execute(
                    "SELECT body FROM intraday_contexts WHERE run_id=?", (run_id,)
                ).fetchone()
                result = _validate_entry_economics_from_context(
                    order,
                    fresh_market,
                    decision,
                    context,
                    settings.strategy,
                    settings.risk,
                )
            database.risk(run_id, "preview", result)
            if not result.allowed:
                database.finish(run_id, "blocked")
                return {"run_id": run_id, "status": "blocked", "risk": result}
            database.preview(run_id, order)
            return {
                "run_id": run_id,
                "mode": settings.mode,
                "status": "preview",
                "order": order,
                "decision": decision,
                "risk": result,
                "submitted": False,
            }
        except AgentError as exc:
            database.finish(run_id, "failed", str(exc))
            raise
        except Exception:
            database.finish(run_id, "failed", "Unexpected internal failure; no automatic submission")
            raise


def execute_preview(
    settings: Settings, broker, database: Database, run_id: str, explicit: bool = False
) -> dict:
    if not explicit:
        raise AgentError("Execution requires the explicit flag for the selected environment")
    if settings.mode == "paper" and settings.paper["trading_enabled"] is not True:
        raise AgentError("Paper execution disabled: set trading_enabled locally before creating a preview")
    with database.lock():
        if broker.mode != settings.mode:
            raise AgentError("Broker mode does not match configuration")
        verify_broker_identity(broker, database)
        run = database.get_run(run_id)
        row = database.order_for_run(run_id)
        if not row:
            raise AgentError("Run has no persisted order preview")
        if run["mode"] != settings.mode or run["config_digest"] != settings.digest:
            raise AgentError("Configuration/environment changed; create a new preview")
        order = intent_from_json(row["intent"])
        # Duplicate execute only queries, including after expiry or process restart.
        if row["attempted"]:
            result = execute_order(order, broker, database)
            return {"execution": result, "reconciliation": reconcile(broker, database)}
        if _unresolved(database):
            raise AgentError("Another submitted order remains unresolved")
        markets = _markets(
            broker,
            settings.paper["symbols"],
            order.symbol,
            settings.risk["max_data_age_seconds"],
            settings.paper["fresh_market_wait_seconds"],
        )
        market = markets[order.symbol]
        prices = {key: value.price for key, value in markets.items()}
        portfolio, asset = broker.get_portfolio(), broker.get_asset_rules(order.symbol)
        database.bind("account_id", portfolio.account_id)
        try:
            daily = database.daily_baseline(utcnow())
        except AgentError:
            # Do not let yesterday's preview establish today's loss baseline.
            raise AgentError("Preview crossed UTC date; create a fresh preview") from None
        decision = run["decision"]
        risk = validate_order(
            order, decision, market, portfolio, asset, settings.risk, daily, market_prices=prices
        )
        if risk.allowed:
            risk = validate_intraday_order(order, market, portfolio, settings.strategy, settings.risk)
        if risk.allowed and "intraday_require_cost_cover" in settings.strategy:
            context = database.connection.execute(
                "SELECT body FROM intraday_contexts WHERE run_id=?", (run_id,)
            ).fetchone()
            risk = _validate_entry_economics_from_context(
                order,
                market,
                decision,
                context,
                settings.strategy,
                settings.risk,
            )
        database.risk(run_id, "execute", risk)
        if not risk.allowed:
            database.finish(run_id, "blocked")
            return {"run_id": run_id, "status": "blocked", "risk": risk}
        fresh = plan_orders(
            decision,
            market,
            portfolio,
            asset,
            settings.risk,
            order.client_order_id,
            market_prices=prices,
        )
        # Validate the ORIGINAL quantity/limit above; don't invalidate a safe
        # reviewed order merely because a new quote rounds a fresh plan differently.
        # The target must still require an order in the same direction.
        if not fresh or fresh[0].side != order.side:
            raise AgentError("Preview sizing changed with account/market; create a new preview")
        if (
            abs(market.price / decimal(json.loads(run["market_json"])["price"]) - 1) * 10000
            > settings.risk["slippage_bps"]
        ):
            raise AgentError("Price moved beyond preview tolerance; create a new preview")

        def before_submit(current_portfolio):
            final_risk = validate_order(
                order,
                decision,
                market,
                current_portfolio,
                asset,
                settings.risk,
                daily,
                market_prices=prices,
            )
            if final_risk.allowed:
                final_risk = validate_intraday_order(
                    order, market, current_portfolio, settings.strategy, settings.risk
                )
            context = database.connection.execute(
                "SELECT body FROM intraday_contexts WHERE run_id=?", (run_id,)
            ).fetchone()
            if final_risk.allowed and "intraday_require_cost_cover" in settings.strategy:
                final_risk = _validate_entry_economics_from_context(
                    order,
                    market,
                    decision,
                    context,
                    settings.strategy,
                    settings.risk,
                )
            if final_risk.allowed and context and "intraday_max_bar_age_seconds" in settings.strategy:
                # Only bars closed when the decision started informed its signal.
                # A raw still-open bar cannot become new evidence while reads retry.
                if not _intraday_bars_current(json.loads(context[0]), decision, settings):
                    final_risk = RiskResult(False, ("Intraday minute bars are stale",))
            if final_risk.allowed:
                entry_reasons = validate_entry_preview(database, settings, run_id, decision)
                if entry_reasons:
                    final_risk = RiskResult(False, tuple(entry_reasons))
            database.risk(run_id, "pre_submit", final_risk)
            return final_risk

        result = execute_order(order, broker, database, before_submit=before_submit)
        if result.get("status") == "blocked":
            database.finish(run_id, "blocked")
            return {"run_id": run_id, "status": "blocked", "risk": result["risk"]}
        return {"run_id": run_id, "execution": result, "reconciliation": reconcile(broker, database)}


def reconcile_account(broker, database: Database, *, refresh_terminal: bool = False) -> dict:
    with database.lock():
        return reconcile(broker, database, refresh_terminal=refresh_terminal)


def cancel_order(broker, database: Database, client_order_id: str, explicit: bool = False) -> dict:
    if not explicit:
        raise AgentError("Cancel requires the explicit execution flag")
    with database.lock():
        verify_broker_identity(broker, database)
        row = next(
            (
                row
                for row in database.orders(attempted_only=True)
                if row["client_order_id"] == client_order_id
            ),
            None,
        )
        if not row:
            raise AgentError("Only locally tracked submitted orders can be canceled")
        broker.cancel_order(client_order_id)
        # A cancel request is not proof of cancellation or proof of no fills.
        return reconcile(broker, database)
