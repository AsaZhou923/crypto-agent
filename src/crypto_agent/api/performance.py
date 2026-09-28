"""Read-only, cash-flow-adjusted Paper performance from closed one-minute buckets.

Alpaca timestamps label bucket START; equity/cashflow describe bucket END.
Never substitute account.equity or the provider's rounded profit_loss_pct.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from zoneinfo import ZoneInfo

from crypto_agent.api.metrics import max_drawdown, performance, tokyo_day_start
from crypto_agent.models import AgentError, decimal, timestamp

STEP = timedelta(minutes=1)
EXTERNAL_CASH = {"CSD", "CSW", "ACATC"}
# Income and execution costs remain part of equity P&L, not external capital.
INTERNAL = {
    "FILL",
    "FEE",
    "CFEE",
    "DIV",
    "DIVCGL",
    "DIVCGS",
    "DIVROC",
    "DIVNRA",
    "DIVFT",
    "DIVTXEX",
    "DIVTX",
    "CGD",
    "INT",
    "INTNRA",
    "PTC",
    "PTR",
    "TAX",
    "SPLIT",
}


def unavailable(reason):
    return {"value": None, "percent": None, "basis": reason, "subtitle": reason}


def unavailable_metrics(reason):
    return {name: unavailable(reason) for name in ("daily", "total", "drawdown")}


def read_activities(broker, start, end):
    """Audit all activity types, including non-cash transfers absent from cashflow."""
    params = {"after": start.isoformat(), "until": end.isoformat(), "direction": "asc", "page_size": 100}
    records, seen = [], set()
    for _ in range(50):
        page = broker._request("GET", "/v2/account/activities", params=params)
        if not isinstance(page, list):
            raise AgentError("资金流水响应无效，无法核对入出金。")
        for item in page:
            if not item.get("id") or item["id"] in seen:
                raise AgentError("资金流水分页不完整，暂不计算收益。")
            seen.add(item["id"])
            records.append(item)
        if len(page) < 100:
            return records
        params["page_token"] = page[-1]["id"]
    raise AgentError("资金流水超过 5,000 条读取上限，无法证明区间完整。")


def parse_history(payload, now):
    if payload.get("timeframe") != "1Min":
        raise AgentError("权益历史采样周期不是 1 分钟。")
    times, values = payload.get("timestamp"), payload.get("equity")
    if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
        raise AgentError("权益历史时间与金额数组不完整。")
    flows = payload.get("cashflow", {})
    if not isinstance(flows, dict) or any(
        not isinstance(v, list) or len(v) != len(times) for v in flows.values()
    ):
        raise AgentError("权益历史资金流数组不完整。")
    points, previous = [], None
    for i, raw_time in enumerate(times):
        if type(raw_time) is not int:
            raise AgentError("权益历史时间戳无效。")
        opened = datetime.fromtimestamp(raw_time, UTC)
        if previous is not None and opened <= previous:
            raise AgentError("权益历史时间重复或乱序。")
        previous = opened
        ended = opened + STEP
        if ended > now:
            continue  # An unfinished bucket is not a midnight baseline or P&L endpoint.
        cash = {kind: decimal(amounts[i]) for kind, amounts in flows.items()}
        if cash.get("CSD", 0) < 0 or cash.get("CSW", 0) > 0:
            raise AgentError("入出金方向与平台类型不一致。")
        points.append(
            {
                "at": ended,
                "value": decimal(values[i]) if values[i] is not None else None,
                "flows": cash,
                "net_flow": sum((cash.get(k, Decimal(0)) for k in EXTERNAL_CASH), Decimal(0)),
            }
        )
    return points


def overlaps(activity, start, end):
    if activity.get("transaction_time"):
        return start < timestamp(activity["transaction_time"]) <= end
    day = activity.get("date")
    if not day or len(day) != 10:
        return True
    # Date-only settlement events have no exact timezone/instant. Keep a broad
    # window covering all civil timezones rather than inventing a posting time.
    day = timestamp(day + "T00:00:00Z")
    return day - timedelta(hours=14) <= end and day + timedelta(hours=38) > start


def calculate(points, activities, *, start, daily=False, drawdown=False):
    candidates = [p for p in points if p["at"] >= start]
    if daily:
        if not candidates or candidates[0]["at"] != start:
            return unavailable("缺少结束于 Asia/Tokyo 午夜的权益基准。")
    else:
        # A zero prefunding period is not a return denominator. Show the actual
        # first funded observation as the interval start, never account lifetime.
        while candidates and candidates[0]["value"] == 0:
            candidates.pop(0)
    if len(candidates) < 2:
        return unavailable("尚无两次完整的已闭合权益观察。")
    first, last = candidates[0], candidates[-1]
    if any(p["value"] is None or p["value"] <= 0 for p in candidates):
        return unavailable("区间权益缺失或非正，无法计算可靠收益率。")
    if any(b["at"] - a["at"] != STEP for a, b in pairwise(candidates)):
        return unavailable("权益历史存在缺失分钟，无法计算完整区间。")
    unclassified = [
        (p["at"], kind, amount)
        for p in candidates[1:]
        for kind, amount in p["flows"].items()
        if amount and kind not in EXTERNAL_CASH | INTERNAL
    ]
    if unclassified:
        details = "; ".join(
            f"{at.astimezone(ZoneInfo('Asia/Tokyo')):%m/%d %H:%M} JST {kind} {amount:+} USD"
            for at, kind, amount in unclassified[:3]
        )
        result = unavailable(
            "平台权益历史含未核对的资金/资产调整："
            + details
            + "。无法确认是入出金、费用还是内部调整；不能仅因金额互相抵消就视为不影响收益。"
        )
        result["subtitle"] = f"平台有 {len(unclassified)} 笔未核对资金调整，见统计口径"
        return result
    audited_flows = {kind: Decimal(0) for kind in EXTERNAL_CASH}
    for activity in activities:
        kind = activity.get("activity_type")
        if kind in INTERNAL or not overlaps(activity, first["at"], last["at"]):
            continue
        if kind not in EXTERNAL_CASH:
            return unavailable("存在无法定价或分类的转移活动：" + str(kind))
        if activity.get("net_amount") is None:
            return unavailable("外部资金活动缺少金额，无法核对入出金。")
        audited_flows[kind] += decimal(activity["net_amount"])
    for kind, amount in audited_flows.items():
        historical = sum((p["flows"].get(kind, Decimal(0)) for p in candidates[1:]), Decimal(0))
        if amount != historical:
            return unavailable("资金流水与权益历史不一致：" + kind + "；可能存在结算日期边界或历史延迟。")

    def local(t):
        return t.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%m/%d %H:%M")

    interval = f"{local(first['at'])}–{local(last['at'])} JST"
    basis = (
        "Alpaca Paper 已闭合 1 分钟权益与 ALL 资金流；"
        + interval
        + f"。期初权益 {first['value']} USD，期末权益 {last['value']} USD。"
        + "这是平台历史估值口径，可能因估值更新或费用迟记而与实时账户余额不同。"
    )
    if drawdown:
        if any(p["value"] - p["net_flow"] <= 0 for p in candidates[1:]):
            return unavailable("桶末资金流调整后净值非正，无法可靠计算采样回撤。")
        result = max_drawdown(
            candidates,
            flows_complete=True,
            basis=basis + "资金按桶末调整后的采样净值峰谷回撤；不代表分钟内的连续最大回撤。",
        )
    else:
        flows = sum((p["net_flow"] for p in candidates[1:]), Decimal(0))
        result = performance(
            first["value"],
            last["value"],
            flows,
            complete=True,
            basis=basis
            + "(期末权益−期初权益−净外部入金)，百分比以期初权益为分母；非时间加权收益率。平台已计入历史权益的费用和收益保留在损益中；迟记费用尚未反映。",
        )
    result.update(
        as_of=last["at"].isoformat(),
        subtitle=interval + " · 平台历史估值",
        source="Alpaca Paper 权益历史",
        stale=False,
        opening_equity=str(first["value"]),
        closing_equity=str(last["value"]),
    )
    return result


def load_performance(broker, now, tracking_start=None):
    """Bounded <=6-day minute history reads and paginated activity audit."""
    now = timestamp(now)
    midnight = tokyo_day_start(now).astimezone(UTC)
    earliest = now - timedelta(days=29)  # API intraday history must be <30 days.
    total_start = max(timestamp(tracking_start), earliest) if tracking_start else earliest
    start = min(total_start, midnight - STEP).replace(second=0, microsecond=0)
    end = now.replace(second=0, microsecond=0)
    # Alpaca rejects 1Min history spans longer than seven days. Split on
    # minute boundaries; bucket-end ownership avoids overlap or double funding.
    points = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=6), end)
        payload = broker._request(
            "GET",
            "/v2/account/portfolio/history",
            params={
                "start": cursor.isoformat(),
                "end": chunk_end.isoformat(),
                "timeframe": "1Min",
                "intraday_reporting": "continuous",
                "pnl_reset": "no_reset",
                "cashflow_types": "ALL",
            },
        )
        points.extend(
            point for point in parse_history(payload, chunk_end) if cursor < point["at"] <= chunk_end
        )
        cursor = chunk_end
    activities = read_activities(broker, start, end)
    # Date-only asset transfers can straddle the exact query boundary and may
    # have zero cashflow. Audit a wider window for non-cash/unknown activities.
    # Cash journals remain checked by the exact-window audit and ALL cashflow.
    seen = {a["id"] for a in activities}
    for activity in read_activities(broker, start - timedelta(days=2), now + timedelta(days=2)):
        if activity["id"] not in seen and activity.get("activity_type") not in INTERNAL | EXTERNAL_CASH | {
            "JNLC"
        }:
            activities.append(activity)
    metrics = {
        "daily": calculate(points, activities, start=midnight, daily=True),
        "total": calculate(points, activities, start=total_start),
        "drawdown": calculate(points, activities, start=total_start, drawdown=True),
    }
    for value in metrics.values():
        if value.get("as_of") and now - timestamp(value["as_of"]) > timedelta(minutes=3):
            value["stale"] = True
            value["subtitle"] += " · 平台权益历史延迟"
        value["basis"] += " 累计/回撤以实际显示区间为准，最多近 29 天；平台历史可能延迟，统计每 60 秒更新。"
    return metrics
