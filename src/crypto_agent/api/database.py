"""Allowlisted read-only views of the existing agent schema; no migrations."""

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from crypto_agent.models import AgentError, decimal, timestamp


def read_database(path: Path) -> dict:
    if not path.is_file():
        raise AgentError("所选本地数据库尚未建立；请确认现有 Paper 配置与账本路径。监控台不会创建交易账本。")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            metadata = dict(db.execute("SELECT key,value FROM metadata")) if "metadata" in tables else {}
            if metadata.get("mode") not in {None, "paper"}:
                raise AgentError("数据库来源不是 Paper；请使用 Paper 账本，演示只能显式 --demo。")
            if metadata.get("mode") is None and tables & {
                "runs",
                "decisions",
                "account_snapshots",
                "auto_cycles",
            }:
                raise AgentError("数据库缺少 Paper 来源绑定，不能确认记录来源。")
            queries = {
                "runs": "SELECT id,created_at,status,market_json,portfolio_json,error FROM runs ORDER BY created_at DESC LIMIT 500",
                "decisions": "SELECT d.run_id,d.body FROM decisions d JOIN runs r ON r.id=d.run_id ORDER BY r.created_at DESC LIMIT 500",
                "risk": "SELECT run_id,phase,created_at,body FROM risk_results ORDER BY id DESC LIMIT 3000",
                "orders": "SELECT client_order_id,run_id,broker_json FROM orders ORDER BY updated_at DESC LIMIT 2000",
                "snapshots": "SELECT observed_at,portfolio_json FROM account_snapshots ORDER BY id DESC LIMIT 2000",
                "cycles": "SELECT id,started_at,ended_at,status,run_id,body FROM auto_cycles ORDER BY started_at DESC LIMIT 500",
            }
            result = {"metadata": metadata}
            names = {"risk": "risk_results", "snapshots": "account_snapshots", "cycles": "auto_cycles"}
            for key, sql in queries.items():
                result[key] = [dict(row) for row in db.execute(sql)] if names.get(key, key) in tables else []
            return result
    except (sqlite3.Error, ValueError, TypeError):
        raise AgentError("本地数据库读取失败；检查路径、权限或账本结构。") from None


def association(db, account_id):
    bound = db["metadata"].get("account_id")
    return bool(account_id and bound and account_id == bound)


def order_links(db, account_id):
    if not association(db, account_id):
        return {}, {}
    clients, brokers = {}, {}
    for row in db["orders"]:
        clients[row["client_order_id"]] = row["run_id"]
        broker = json.loads(row["broker_json"]) if row["broker_json"] else {}
        if broker.get("broker_order_id"):
            brokers[broker["broker_order_id"]] = row["run_id"]
    return clients, brokers


def agent_view(db, account_id):
    runs = {run["id"]: run for run in db["runs"]}
    _, brokers = order_links(db, account_id)
    decisions = []
    for row in db["decisions"]:
        raw = json.loads(row["body"])
        run = runs.get(row["run_id"], {})
        risk = []
        for item in reversed(db["risk"]):
            if item["run_id"] == row["run_id"]:
                body = json.loads(item["body"])
                risk.append(
                    {
                        "phase": item["phase"],
                        "created_at": item["created_at"],
                        "allowed": body["allowed"],
                        "reasons": body.get("reasons", []),
                    }
                )
        current = None
        portfolio = json.loads(run["portfolio_json"]) if run.get("portfolio_json") else None
        market = json.loads(run["market_json"]) if run.get("market_json") else None
        if portfolio and market:
            symbol = raw.get("symbol")
            quote = market if market.get("symbol") == symbol else market.get(symbol)
            if quote and quote.get("price") is not None and decimal(portfolio["equity_usd"]) > 0:
                qty = sum(
                    (decimal(p["quantity"]) for p in portfolio["positions"] if p["symbol"] == symbol),
                    Decimal(0),
                )
                current = str(qty * decimal(quote["price"]) / decimal(portfolio["equity_usd"]))
        allowed = (
            "symbol",
            "created_at",
            "expires_at",
            "rating",
            "strategy_version",
            "model",
            "target_position_pct",
            "reason",
            "evidence",
            "actionable",
        )
        saved = {key: raw.get(key) for key in allowed if key in raw}
        decisions.append(
            {
                **saved,
                "run_id": row["run_id"],
                "current_position_pct": current,
                "risk": risk,
                "order_ids": [key for key, value in brokers.items() if value == row["run_id"]],
                "raw": saved,
            }
        )
    logs = [
        {
            "id": run["id"],
            "run_id": run["id"],
            "phase": "run",
            "status": run["status"],
            "started_at": run["created_at"],
            "duration_ms": None,
            "error": run["error"],
        }
        for run in db["runs"]
    ]
    for i, item in enumerate(db["risk"]):
        body = json.loads(item["body"])
        logs.append(
            {
                "id": f"risk-{i}-{item['run_id']}",
                "run_id": item["run_id"],
                "phase": item["phase"],
                "status": "passed" if body["allowed"] else "blocked",
                "started_at": item["created_at"],
                "duration_ms": None,
                "error": None if body["allowed"] else "；".join(body.get("reasons", [])),
            }
        )
    # Runs lack end times, but automation cycles persist both timestamps.
    latest = [(timestamp(run["created_at"]), run["status"]) for run in db["runs"]]
    for cycle in db.get("cycles", []):
        started = timestamp(cycle["started_at"])
        ended = timestamp(cycle["ended_at"]) if cycle["ended_at"] else None
        duration = int((ended - started).total_seconds() * 1000) if ended and ended >= started else None
        body = json.loads(cycle["body"]) if cycle["body"] else {}
        messages = []
        if isinstance(body, dict):
            for key in ("error", "reason", "message", "warning", "warnings"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    messages.append(value)
                elif isinstance(value, list):
                    messages.extend(item for item in value if isinstance(item, str) and item)
            if body.get("automatic_paused") is True:
                messages.append("自动循环已暂停（已保存状态）。")
        logs.append(
            {
                "id": "cycle-" + cycle["id"],
                "run_id": cycle["run_id"],
                "phase": "auto_cycle",
                "status": cycle["status"],
                "started_at": cycle["started_at"],
                "duration_ms": duration,
                "error": "；".join(messages) or None,
            }
        )
        latest.append((ended or started, cycle["status"]))
    last_at, status = max(latest, key=lambda item: item[0]) if latest else (None, "no_runs")
    notice = (
        "最近 500 次运行与 500 次自动周期；日志来自已保存的运行、风控和 auto_cycles。"
        "自动周期耗时按 ended_at−started_at 计算；未结束周期与无结束时间的单独运行耗时未知。"
        "最后运行/周期记录超过 30 分钟即标记过期；读取数据库不代表 Agent 仍在运行。"
    )
    if not latest:
        notice += "所选账本暂无已保存运行或自动周期。"
    notice += (
        "账户身份已匹配，关联使用保存的订单 ID。"
        if association(db, account_id)
        else "账户绑定尚未验证或不匹配，暂不关联平台订单/成交；此区仅展示所选本地账本。"
    )
    return {
        "decisions": decisions,
        "logs": sorted(logs, key=lambda row: timestamp(row["started_at"]), reverse=True),
        "status": status,
        "last_run_at": last_at.isoformat() if last_at else None,
        "notice": notice,
    }


def equity_view(db, account_id):
    if not association(db, account_id):
        return {"points": [], "basis": "账户绑定尚未验证或不匹配，不混用本地快照与当前平台账户。"}
    points = {}
    for row in reversed(db["snapshots"]):
        portfolio = json.loads(row["portfolio_json"])
        if portfolio.get("account_id") == account_id:
            points[row["observed_at"]] = str(decimal(portfolio["equity_usd"]))
    return {
        "points": [{"time": key, "value": points[key]} for key in sorted(points)],
        "basis": "所选 Paper 账本最近 2,000 个账户权益快照；稀疏观察且未作资金流调整，不是收益曲线。",
    }
