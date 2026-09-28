"""Cash-flow-aware performance helpers; never turn missing evidence into zero."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from crypto_agent.models import decimal, timestamp


def tokyo_day_start(value: str | datetime) -> datetime:
    return (
        timestamp(value).astimezone(ZoneInfo("Asia/Tokyo")).replace(hour=0, minute=0, second=0, microsecond=0)
    )


def performance(opening, closing, net_flows, *, complete: bool, basis: str) -> dict:
    """Absolute P&L excludes net funding; percent uses opening equity (not TWR).

    Funding timing is not known here, so the denominator is explicitly opening
    equity. This is a simple funding-adjusted return, never a time-weighted return.
    """
    if not complete or opening is None or closing is None or net_flows is None:
        return {"value": None, "percent": None, "basis": basis}
    initial = decimal(opening)
    if initial <= 0:
        return {"value": None, "percent": None, "basis": basis}
    pnl = decimal(closing) - initial - decimal(net_flows)
    return {"value": str(pnl), "percent": str(pnl / initial), "basis": basis}


def max_drawdown(points: list, *, flows_complete: bool, basis: str) -> dict:
    """Points carry equity and net flow since the previous observed point.

    Unitize at observed flows (flow assumed at period end). Without a complete
    flow series the result is unavailable. This is sampled, not intraday maximum.
    """
    if not flows_complete or len(points) < 2:
        return {"value": None, "percent": None, "basis": basis}
    previous = decimal(points[0]["value"])
    if previous <= 0:
        return {"value": None, "percent": None, "basis": basis}
    index = peak = Decimal(1)
    worst = Decimal(0)
    for point in points[1:]:
        equity = decimal(point["value"])
        flow = decimal(point.get("net_flow", 0))
        if previous <= 0:
            return {"value": None, "percent": None, "basis": basis}
        index *= (equity - flow) / previous
        peak = max(peak, index)
        worst = min(worst, index / peak - 1)
        previous = equity
    return {"value": None, "percent": str(worst), "basis": basis}
