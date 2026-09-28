"""Bounded crypto/USD spot simulation; execution always requires a saved preview."""

import argparse
import importlib.metadata
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from crypto_agent.config import load_settings
from crypto_agent.data.market import validate_snapshot
from crypto_agent.models import AgentError, dumps
from crypto_agent.runner import (
    cancel_order,
    execute_preview,
    make_broker,
    reconcile_account,
    run_once,
)
from crypto_agent.storage.database import Database


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config", type=Path, default=Path("config"), help="Directory containing all three YAML files"
    )
    result.add_argument("--mode", choices=("paper", "offline"), default="paper")
    result.add_argument("--db", type=Path, help="Override local ledger path (mode/account bound)")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status"):
        doctor = commands.add_parser(name, help="Validate configuration and installed integrations")
        doctor.add_argument("--connect", action="store_true", help="GET-only broker connection check")
    for name in ("market", "analyze", "preview", "run"):
        command = commands.add_parser(name)
        command.add_argument("--symbol", help="Configured crypto/USD symbol; defaults to first")
    for name in ("account", "positions", "history", "orders", "report"):
        commands.add_parser(name)
    reconcile_command = commands.add_parser("reconcile")
    reconcile_command.add_argument(
        "--refresh-terminal",
        action="store_true",
        help="Explicit audit: also re-query every historical terminal order (may hit rate limits)",
    )
    commands.add_parser("demo", help="Synthetic offline full loop; never connects to Alpaca or LLMs")
    execute = commands.add_parser("execute", help="Execute one saved preview, after fresh risk validation")
    execute.add_argument("run_id")
    execute.add_argument("--execute-paper", action="store_true")
    execute.add_argument("--execute-offline", action="store_true")
    cancel = commands.add_parser(
        "cancel", help="Cancel a tracked order then query authoritative broker state"
    )
    cancel.add_argument("client_order_id")
    cancel.add_argument("--execute-paper", action="store_true")
    cancel.add_argument("--execute-offline", action="store_true")
    for name in ("auto-enable", "auto-tick"):
        automatic = commands.add_parser(name, help="Explicitly enable or run one bounded scheduled cycle")
        automatic.add_argument("--execute-paper", action="store_true")
        automatic.add_argument("--execute-offline", action="store_true")
    commands.add_parser("auto-status", help="Show persisted cadence, pause status and optimization evidence")
    commands.add_parser("auto-pause", help="Immediately prevent further automatic submissions")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    broker = database = None
    try:
        if args.command == "demo":
            args.mode = "offline"
            if args.config == Path("config"):
                args.config = Path("config/demo")
        settings = load_settings(args.config, args.mode)
        if args.db:
            settings = replace(settings, paper={**settings.paper, "database_path": str(args.db.resolve())})
        if args.command in {"doctor", "status"}:
            result = {
                "project": "crypto-agent",
                "version": importlib.metadata.version("crypto-agent"),
                "mode": settings.mode,
                "config_valid": True,
                "config_digest": settings.digest,
                "execution_enabled_in_config": settings.paper["trading_enabled"],
                "alpaca_credentials_present": all(
                    os.environ.get(key) for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
                ),
                "model_configured": all(
                    settings.strategy.get(key)
                    for key in ("llm_provider", "deep_think_llm", "quick_think_llm")
                ),
                "strategy": settings.strategy["name"],
                "upstream_commit": settings.strategy["upstream_commit"],
                "database_path": str(settings.database_path),
                "connection_checked": False,
            }
            try:
                result["tradingagents_installed_version"] = importlib.metadata.version("tradingagents")
            except importlib.metadata.PackageNotFoundError:
                result["tradingagents_installed_version"] = None
            if args.connect:
                broker = make_broker(settings)
                # No POST/DELETE path reachable from this command.
                markets = broker.get_markets(tuple(settings.paper["symbols"]))
                portfolio = broker.get_portfolio()
                assets = {symbol: broker.get_asset_rules(symbol) for symbol in settings.paper["symbols"]}
                from crypto_agent.risk.checks import validate_inputs

                prices = {key: value.price for key, value in markets.items()}
                checks = {
                    symbol: validate_inputs(
                        None,
                        markets[symbol],
                        portfolio,
                        assets[symbol],
                        settings.risk,
                        portfolio.equity_usd,
                        market_prices=prices,
                    )
                    for symbol in settings.paper["symbols"]
                }
                result.update(
                    connection_checked=True,
                    connection_read_only=True,
                    account_tradable=portfolio.tradable,
                    configured_symbols=settings.paper["symbols"],
                    data_checks=checks,
                    asset_rules=assets,
                )
        else:
            database = Database(settings.database_path, settings.mode)
            if args.command == "history":
                result = database.recent_runs()
            elif args.command == "orders":
                result = [
                    {
                        **row,
                        "intent": json.loads(row["intent"]),
                        "broker_json": json.loads(row["broker_json"]) if row["broker_json"] else None,
                    }
                    for row in database.orders()
                ]
            elif args.command == "report":
                result = database.report()
            elif args.command.startswith("auto-"):
                from crypto_agent.automation import Automation

                automatic = Automation(settings, database)
                explicit = (settings.mode == "paper" and getattr(args, "execute_paper", False)) or (
                    settings.mode == "offline" and getattr(args, "execute_offline", False)
                )
                if (getattr(args, "execute_paper", False) and settings.mode != "paper") or (
                    getattr(args, "execute_offline", False) and settings.mode != "offline"
                ):
                    raise AgentError("Execution flag does not match selected environment")
                if args.command == "auto-status":
                    result = automatic.status()
                elif args.command == "auto-pause":
                    result = automatic.pause()
                elif args.command == "auto-enable":
                    result = automatic.enable(explicit=explicit)
                else:
                    broker = make_broker(
                        settings, allow_submit=bool(explicit and settings.paper["trading_enabled"])
                    )
                    result = automatic.tick(broker, explicit=explicit)
            else:
                explicit = (settings.mode == "paper" and getattr(args, "execute_paper", False)) or (
                    settings.mode == "offline" and getattr(args, "execute_offline", False)
                )
                if (getattr(args, "execute_paper", False) and settings.mode != "paper") or (
                    getattr(args, "execute_offline", False) and settings.mode != "offline"
                ):
                    raise AgentError("Execution flag does not match selected environment")
                allow_submit = bool(explicit and settings.paper["trading_enabled"])
                broker = make_broker(settings, allow_submit=allow_submit)
                if args.command == "market":
                    symbol = args.symbol or settings.paper["symbols"][0]
                    market = broker.get_market(symbol)
                    validate_snapshot(
                        market,
                        max_age_seconds=settings.risk["max_data_age_seconds"],
                        max_spread_bps=settings.risk["max_spread_bps"],
                    )
                    if not settings.risk["min_price_usd"] <= market.price <= settings.risk["max_price_usd"]:
                        raise AgentError("Market price is outside configured sanity bounds")
                    result = {
                        "mode": settings.mode,
                        "market": market,
                        "asset": broker.get_asset_rules(symbol),
                    }
                elif args.command in {"account", "positions"}:
                    result = {"mode": settings.mode, "portfolio": broker.get_portfolio()}
                elif args.command in {"preview", "run", "analyze"}:
                    result = run_once(
                        settings,
                        broker,
                        database,
                        analysis_only=args.command == "analyze",
                        symbol=args.symbol,
                    )
                elif args.command == "execute":
                    result = execute_preview(settings, broker, database, args.run_id, explicit)
                elif args.command == "cancel":
                    if settings.mode == "paper" and not settings.paper["trading_enabled"]:
                        raise AgentError("Paper execution/cancellation is disabled in local configuration")
                    result = cancel_order(broker, database, args.client_order_id, explicit)
                elif args.command == "reconcile":
                    result = reconcile_account(broker, database, refresh_terminal=args.refresh_terminal)
                elif args.command == "demo":
                    preview = run_once(settings, broker, database)
                    execution = (
                        execute_preview(settings, broker, database, preview["run_id"], True)
                        if preview["status"] == "preview"
                        else None
                    )
                    result = {
                        "notice": "SYNTHETIC TEST DATA + LOCAL OFFLINE BROKER. No network or real capital.",
                        "preview": preview,
                        "execution": execution,
                        "report": database.report(),
                    }
                else:
                    raise AgentError("Unsupported command")
        print(dumps(result))
        checked = args.command in {
            "doctor",
            "status",
            "analyze",
            "preview",
            "run",
            "execute",
            "demo",
            "reconcile",
            "cancel",
            "auto-tick",
        }
        return 2 if checked and _failed_outcome(result) else 0
    except AgentError as exc:
        print(
            dumps(
                {
                    "error": str(exc),
                    "submitted_by_this_command": False
                    if args.command not in {"execute", "cancel", "auto-tick"}
                    else "check_order_ledger",
                }
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        # Third-party exceptions can embed credentials/request bodies. Never echo them.
        print(
            dumps(
                {
                    "error": "Unexpected internal failure; inspect tests and reconcile order state before further execution"
                }
            ),
            file=sys.stderr,
        )
        return 3
    finally:
        if broker is not None:
            broker.close()
        if database is not None:
            database.close()


def _failed_outcome(value) -> bool:
    if isinstance(value, dict):
        if (
            value.get("status") in {"blocked", "failed", "unknown", "rejected", "halted"}
            or value.get("allowed") is False
        ):
            return True
        return any(_failed_outcome(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_failed_outcome(item) for item in value)
    # BrokerOrder/RiskResult dataclasses are otherwise handled by JSON output.
    return (
        getattr(value, "status", None) in {"rejected", "unknown"} or getattr(value, "allowed", None) is False
    )


if __name__ == "__main__":
    raise SystemExit(main())
