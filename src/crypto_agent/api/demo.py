"""Deterministic isolated monitor fixtures. Never touches a broker or database."""

import math
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from crypto_agent.api.metrics import max_drawdown, performance
from crypto_agent.models import timestamp

CLOCK = timestamp("2026-09-19T12:00:00Z")
NOTICE = "演示数据 · 固定时钟 2026-09-19 21:00 Asia/Tokyo；所有金额、订单、成交均为可重复的合成示例。"


def iso(minutes=0):
    return (CLOCK + timedelta(minutes=minutes)).isoformat()


def section(data, source="离线演示", at=None):
    return {
        "source": source,
        "as_of": at or iso(),
        "stale": False,
        "error": None,
        "data": data,
        "notice": NOTICE,
    }


def market(timeframe="1Min", *, symbol="BTC/USD"):
    if symbol not in {"BTC/USD", "XRP/USD"}:
        raise ValueError("Unsupported demo market symbol")
    step = {"1Min": 1, "5Min": 5, "1Hour": 60}[timeframe]
    bars = []
    previous = Decimal("64000" if symbol == "BTC/USD" else "0.58")
    upper_wick = Decimal("62" if symbol == "BTC/USD" else ".0018")
    lower_wick = Decimal("48" if symbol == "BTC/USD" else ".0012")
    for i in range(180):
        if symbol == "BTC/USD":
            close = Decimal(str(round(64000 + i * 8 + math.sin(i / 11) * 260 + math.sin(i / 3) * 42, 2)))
            if i == 179:
                close = Decimal("65480")
            volume = round(4 + abs(math.sin(i * 1.71)) * 16, 4)
        else:
            close = Decimal(
                str(round(0.58 + i * 0.00012 + math.sin(i / 8) * 0.006 + math.sin(i / 2) * 0.001, 5))
            )
            volume = round(1800 + abs(math.sin(i * 1.23)) * 7500, 2)
        bars.append(
            {
                "time": iso((i - 180) * step),
                "open": str(previous),
                "high": str(max(previous, close) + upper_wick),
                "low": str(min(previous, close) - lower_wick),
                "close": str(close),
                "volume": str(volume),
            }
        )
        previous = close
    return section(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": bars,
            "notice": f"演示数据：合成 {symbol} K 线与成交量；"
            + ("成交价格为独立的合成执行记录。" if symbol == "BTC/USD" else "没有 XRP 演示成交或决策。"),
        },
        at=bars[-1]["time"],
    )


def dashboard():
    trades = [
        ("buy", ".2", "64000", "12.8", -80),
        ("sell", ".05", "65000", "3.25", -45),
        ("buy", ".02", "65200", "1.304", -20),
    ]
    orders, fills, fees, decisions, logs = [], [], [], [], []
    cash, qty, cost = Decimal("100000"), Decimal(0), Decimal(0)
    for i, (side, q, p, f, minute) in enumerate(trades, 1):
        quantity, price, fee = Decimal(q), Decimal(p), Decimal(f)
        run_id, order_id = f"demo-run-{i:03}", f"paper-demo-{i:03}"
        current_pct = qty * price / (cash + qty * price)
        if side == "buy":
            cash -= quantity * price + fee
            cost += quantity * price
            qty += quantity
        else:
            cash += quantity * price - fee
            cost -= quantity * cost / qty
            qty -= quantity
        orders.append(
            {
                "id": order_id,
                "client_order_id": f"demo-client-{i:03}",
                "symbol": "BTC/USD",
                "side": side,
                "quantity": ".05" if i == 3 else q,
                "filled_quantity": q,
                "filled_avg_price": p,
                "status": "partially_filled" if i == 3 else "filled",
                "submitted_at": iso(minute - 1),
                "run_id": run_id,
            }
        )
        fills.append(
            {
                "id": f"demo-fill-{i:03}",
                "order_id": order_id,
                "symbol": "BTC/USD",
                "side": side,
                "quantity": q,
                "price": p,
                "fee": f,
                "occurred_at": iso(minute),
                "run_id": run_id,
            }
        )
        fees.append({"id": f"demo-fee-{i:03}", "order_id": order_id, "amount": f, "occurred_at": iso(minute)})
        decisions.append(
            decision(
                run_id,
                minute - 2,
                "Buy" if side == "buy" else "Underweight",
                "0.20" if side == "buy" else "0.10",
                str(current_pct),
                [order_id],
            )
        )
    decisions.extend(
        [
            decision("demo-run-004", -14, "Buy", "0.35", "0.11", [], rejected=True),
            decision("demo-run-005", -2, "Hold", None, str(qty * 65480 / (cash + qty * 65480)), []),
        ]
    )
    for d in decisions:
        logs.append(
            {
                "id": d["run_id"],
                "run_id": d["run_id"],
                "phase": "decision",
                "status": "blocked" if d["run_id"] == "demo-run-004" else "analyzed",
                "started_at": d["created_at"],
                "duration_ms": 1842,
                "error": None,
            }
        )
    logs.append(
        {
            "id": "demo-data-warning",
            "run_id": None,
            "phase": "market",
            "status": "warning",
            "started_at": iso(-35),
            "duration_ms": 15000,
            "error": "演示历史告警：行情读取超时，随后已恢复。",
        }
    )
    mark = Decimal("65480")
    equity = cash + qty * mark
    points = []
    balance, holding, event_index = Decimal("100000"), Decimal(0), 0
    for minute in range(-120, 1, 2):
        while event_index < len(trades) and minute >= trades[event_index][4]:
            side, q, p, f, _ = trades[event_index]
            dq, dp, df = Decimal(q), Decimal(p), Decimal(f)
            balance += (dq * dp if side == "sell" else -dq * dp) - df
            holding += -dq if side == "sell" else dq
            event_index += 1
        price = Decimal(str(round(64000 + (minute + 80) * 18.5 + math.sin(minute / 9) * 110, 2)))
        if minute == 0:
            price = mark
        points.append(
            {"time": iso(minute), "value": str((balance + holding * price).quantize(Decimal(".000001")))}
        )
    daily = performance(
        "100000",
        equity,
        "0",
        complete=True,
        basis="固定演示日 2026-09-19 00:00–21:00 Asia/Tokyo；(期末权益−日初权益−净入金)/日初权益，含已记费用。",
    )
    total = performance(
        "100000",
        equity,
        "0",
        complete=True,
        basis="演示账户建立至固定时钟；初始入金 100,000 USD，随后无入出金；收益=(权益−初始资本−后续净入金)/初始资本。",
    )
    account = {
        "equity": str(equity),
        "cash": str(cash),
        "buying_power": str(cash),
        "positions": [
            {
                "symbol": "BTC/USD",
                "quantity": str(qty),
                "average_entry_price": str(cost / qty),
                "current_price": str(mark),
                "market_value": str(qty * mark),
                "unrealized_pnl": str(qty * mark - cost),
            }
        ],
        "metrics": {
            "daily": daily,
            "total": total,
            "drawdown": max_drawdown(
                points,
                flows_complete=True,
                basis="固定演示日；按 2 分钟权益采样的最大回撤；无后续资金流，不代表未采样时刻的最大跌幅。",
            ),
        },
    }
    return {
        "mode": "demo",
        "as_of": iso(),
        "timezone": "Asia/Tokyo",
        "sections": {
            "account": section(account),
            "ledger": section(
                {
                    "orders": list(reversed(orders)),
                    "fills": list(reversed(fills)),
                    "fees": fees,
                    "notice": "演示账户完整成交记录；第 3 笔订单委托 0.05 BTC、成交 0.02 BTC；费用已扣现金，请勿重复计入。",
                }
            ),
            "agent": section(
                {
                    "decisions": list(reversed(decisions)),
                    "logs": sorted(logs, key=lambda row: row["started_at"], reverse=True),
                    "status": "analyzed",
                    "last_run_at": iso(-2),
                    "notice": "固定合成决策；Hold 没有目标仓位。",
                }
            ),
            "equity": section(
                {"points": points, "basis": "演示账户权益=现金+按时点价格计值的持仓；独立于 BTC 价格。"}
            ),
        },
    }


def decision(run_id, minute, rating, target, current, orders, rejected=False):
    hold = rating == "Hold"
    raw = {
        "symbol": "BTC/USD",
        "created_at": iso(minute),
        "expires_at": iso(minute + 10),
        "rating": rating,
        "strategy_version": "intraday_10m-demo-v1",
        "model": "demo-model / deterministic",
        "target_position_pct": target,
        "reason": "短期动量仍偏正，但未覆盖双边交易成本；保持当前仓位，等待下一个有效信号。"
        if hold
        else "短周期价格与均线方向一致；依据固定演示规则调整目标敞口。",
        "evidence": ["5 / 20 EMA 方向一致（演示观察）", "风险因素：短周期波动与点差会侵蚀交易优势。"],
        "actionable": True,
    }
    return {
        **raw,
        "run_id": run_id,
        "current_position_pct": current,
        "order_ids": orders,
        "risk": [
            {
                "phase": "decision",
                "allowed": not rejected,
                "reasons": ["目标仓位 35% 超过演示单币种上限 25%。"]
                if rejected
                else ["演示输入有效、无杠杆；仓位与费用预留检查通过。"],
                "created_at": iso(minute),
            }
        ],
        "raw": deepcopy(raw),
    }


def scenario_response(value, scenario, *, is_market=False):
    result = deepcopy(value)
    if scenario == "stale" and not is_market:
        result["as_of"] = iso(30)  # Advance the clock, never backdate observations.
    sections = {"market": result} if is_market else result["sections"]
    for name, item in sections.items():
        if scenario == "empty":
            item["data"] = None
            item["notice"] = "演示空数据场景：暂无已保存数据。"
        elif scenario == "stale":
            item["stale"] = True
            item["notice"] = "演示数据过期场景：保留最后成功数据，当前时钟为 21:30 Asia/Tokyo。"
        elif scenario == "disconnected" or (scenario == "partial" and name in {"ledger", "market"}):
            item["error"] = "演示连接中断：请求超时，显示最后成功数据。"
            item["stale"] = True
    return result
