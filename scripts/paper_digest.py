"""Send one Paper trade digest at each scheduled Tokyo-time slot."""

import fcntl
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_schedule import ROOT, Scheduler, save_json

JST = ZoneInfo("Asia/Tokyo")
HOURS = (9, 21)
FILL = re.compile(r"^([A-Z]+/USD) (buy|sell) 成交增加 (\S+)；状态 ")


def slot_at(now: datetime) -> datetime:
    local = now.astimezone(JST)
    for hour in reversed(HOURS):
        slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot <= local:
            return slot.astimezone(UTC)
    yesterday = local - timedelta(days=1)
    return yesterday.replace(hour=HOURS[-1], minute=0, second=0, microsecond=0).astimezone(UTC)


def previous_slot(end: datetime) -> datetime:
    return slot_at(end - timedelta(microseconds=1))


def audit_events(directory: Path, start: datetime, end: datetime) -> list[dict]:
    files = list(directory.glob("audit.jsonl*"))
    if not files:
        raise RuntimeError("Paper scheduler audit is missing")
    events = []
    for path in files:
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
                at = datetime.fromisoformat(event["at"])
            except (ValueError, KeyError, TypeError):
                continue
            if start <= at < end:
                events.append(event)
    return events


def message_for(events: list[dict], start: datetime, end: datetime) -> str:
    quantities = defaultdict(Decimal)
    rejections = 0
    adjustments = []
    last_status = None
    for event in events:
        if event.get("kind") == "status":
            at = datetime.fromisoformat(event["at"])
            if last_status is None or at > last_status:
                last_status = at
        if event.get("kind") != "event":
            continue
        message = event.get("message", "")
        match = FILL.match(message)
        if match:
            try:
                quantities[(match[1], match[2])] += Decimal(match[3])
            except InvalidOperation:
                continue
        elif message.startswith("订单拒绝："):
            rejections += 1
        elif " 参数倍数调整为 " in message:
            adjustments.append(message)

    local_start, local_end = start.astimezone(JST), end.astimezone(JST)
    lines = [f"Paper 交易汇总（{local_start:%m-%d %H:%M}–{local_end:%m-%d %H:%M} JST）"]
    if quantities:
        lines.append("本时段观察到的成交量：")
        for (symbol, side), quantity in sorted(quantities.items()):
            lines.append(f"{symbol} {'买入' if side == 'buy' else '卖出'} {quantity:f}")
    else:
        lines.append("本时段无新成交。")
    if rejections:
        lines.append(f"订单拒绝：{rejections} 次")
    if adjustments:
        lines.append("策略参数调整：" + "；".join(adjustments[-3:]))
    if last_status is None or end - last_status > timedelta(minutes=30):
        lines.append("调度记录已过期，请核对交易服务与账本。")
    return "\n".join(lines)


def run(root: Path = ROOT, now: datetime | None = None) -> int:
    scheduler = Scheduler(root)
    directory = scheduler.directory
    with (directory / "digest.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        end = slot_at(now or datetime.now(UTC))
        state = directory / "digest-state.json"
        previous = json.loads(state.read_text()) if state.exists() else {}
        last_slot = datetime.fromisoformat(previous["last_slot"]) if previous else None
        if last_slot is not None and last_slot >= end:
            return 0
        start = last_slot or previous_slot(end)
        message = message_for(audit_events(directory, start, end), start, end)
        # Mark the slot before the webhook call: a failure cannot cause a
        # duplicate message on a later invocation.
        save_json(state, {"last_slot": end.isoformat()})
        return 0 if scheduler.notify(message) else 1


if __name__ == "__main__":
    raise SystemExit(run())
