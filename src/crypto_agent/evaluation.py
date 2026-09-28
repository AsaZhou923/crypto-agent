"""Conservative observational replay of fixed rating-to-target policies.

This is not an execution backtest: periodic quotes cannot establish historical
limit fills, intra-interval drawdowns or whether a short-lived decision remained
executable. Signals are applied only to a later observed quote. A promotion may
only reduce the existing exposure multiplier; it never changes risk or models.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from crypto_agent.config import validate_risk
from crypto_agent.models import AgentError, AssetRules, decimal, normalize_symbol, timestamp
from crypto_agent.risk.intraday import probe_position_cap

CANDIDATE_MULTIPLIERS = (Decimal("0.5"), Decimal("0.75"), Decimal("1"))
MIN_OBSERVATIONS = 146
MIN_TRAIN_OBSERVATIONS = 96
MIN_VALIDATION_OBSERVATIONS = 48
MIN_TRAIN_SECONDS = 16 * 60 * 60
MIN_VALIDATION_SECONDS = 8 * 60 * 60
MIN_INTERVAL_SECONDS = 600
MAX_GAP_SECONDS = 1200
BPS = Decimal(10000)
NOTICE = (
    "Observational hypothetical target replay only, using later observed quotes and configured "
    "fee/slippage reserves. Not verified historical order fills, actual execution performance, "
    "or a guarantee of paper or real-money returns. Sparse quotes omit intrainterval risk. "
    "A historical rating is not an executable unexpired live decision."
)


@dataclass(frozen=True)
class Observation:
    observed_at: datetime
    available_at: datetime
    rating: str
    bid: Decimal
    ask: Decimal
    equity: Decimal
    config_digest: str = ""
    strategy_version: str = ""
    model: str = ""
    mode: str = "paper"
    actionable: bool = True
    cycle_started_at: datetime | None = None
    symbol: str = "BTC/USD"


def _round(value: Decimal, increment: Decimal, rounding: str = ROUND_FLOOR) -> Decimal:
    return (value / increment).to_integral_value(rounding=rounding) * increment


def _inputs(
    observations,
    base_targets,
    risk,
    multiplier,
    asset,
    max_gap_seconds=MAX_GAP_SECONDS,
    *,
    min_interval_seconds=MIN_INTERVAL_SECONDS,
):
    if type(min_interval_seconds) is not int or not 60 <= min_interval_seconds <= 3600:
        raise AgentError("Replay minimum interval must be an integer between 60 and 3600 seconds")
    maximum_gap = max(2400, 4 * min_interval_seconds)
    if type(max_gap_seconds) is not int or not min_interval_seconds <= max_gap_seconds <= maximum_gap:
        raise AgentError(
            f"Replay gap allowance must be between {min_interval_seconds} and {maximum_gap} seconds"
        )
    limits = validate_risk(risk)
    multiplier = decimal(multiplier, "exposure multiplier")
    if multiplier not in CANDIDATE_MULTIPLIERS:
        raise AgentError("Exposure multiplier must be one of 0.5, 0.75, 1")
    if not isinstance(base_targets, dict) or set(base_targets) != {
        "Buy",
        "Overweight",
        "Underweight",
        "Sell",
    }:
        raise AgentError("All four original rating targets are required")
    targets = {name: decimal(value, name) for name, value in base_targets.items()}
    if not (
        targets["Sell"]
        == 0
        <= targets["Underweight"]
        <= targets["Overweight"]
        <= targets["Buy"]
        <= limits["max_position_pct"]
    ):
        raise AgentError("Original rating targets exceed risk limits or are unordered")
    if asset.tradable is not True:
        raise AgentError("Replay requires tradable configured asset rules")
    asset_symbol = normalize_symbol(asset.symbol)
    asset = replace(
        asset,
        min_order_size=decimal(asset.min_order_size),
        quantity_increment=decimal(asset.quantity_increment),
        price_increment=decimal(asset.price_increment),
    )
    if min(asset.min_order_size, asset.quantity_increment, asset.price_increment) <= 0:
        raise AgentError("Replay requires positive broker precision and minimums")
    checked = []
    cohort = None
    has_cycle_times = None
    for item in observations:
        if not isinstance(item, Observation):
            raise AgentError("Replay requires explicit timestamped observations")
        item = replace(
            item,
            observed_at=timestamp(item.observed_at),
            available_at=timestamp(item.available_at),
            bid=decimal(item.bid),
            ask=decimal(item.ask),
            equity=decimal(item.equity),
            cycle_started_at=timestamp(item.cycle_started_at) if item.cycle_started_at is not None else None,
            symbol=normalize_symbol(item.symbol),
        )
        if item.symbol != asset_symbol:
            raise AgentError("Replay cannot mix symbols")
        current_has_cycle_time = item.cycle_started_at is not None
        if has_cycle_times is not None and current_has_cycle_time != has_cycle_times:
            raise AgentError("Replay cannot mix known and missing cycle start times")
        has_cycle_times = current_has_cycle_time
        if current_has_cycle_time and item.cycle_started_at > item.available_at:
            raise AgentError("Replay cycle starts after its decision became available")
        if item.rating not in {*targets, "Hold", "REVIEW"} or type(item.actionable) is not bool:
            raise AgentError("Replay contains an invalid decision rating")
        if item.rating in targets and not item.actionable:
            raise AgentError("Replay contains a nonactionable trade rating")
        if item.rating == "REVIEW" and item.actionable:
            raise AgentError("REVIEW cannot be actionable")
        identity = (item.mode, item.config_digest, item.strategy_version, item.model, item.symbol)
        if item.mode not in {"paper", "offline"} or any(
            not isinstance(value, str) or not value.strip() for value in identity
        ):
            raise AgentError("Replay requires complete mode/config/strategy/model provenance")
        if cohort is not None and cohort != identity:
            raise AgentError("Replay cannot mix mode/config/strategy/model cohorts")
        cohort = identity
        bounds = limits["price_bounds_usd"][item.symbol]
        minimum = max(bounds["min"], limits["min_price_usd"])
        maximum = min(bounds["max"], limits["max_price_usd"])
        if not minimum <= item.bid <= item.ask <= maximum:
            raise AgentError("Replay contains an invalid or crossed quote")
        midpoint = (item.ask + item.bid) / 2
        if (item.ask - item.bid) / midpoint * BPS > limits["max_spread_bps"]:
            raise AgentError("Replay quote spread exceeds risk limit")
        if item.equity <= 0:
            raise AgentError("Replay requires observed positive account equity")
        if (
            abs(decimal((item.available_at - item.observed_at).total_seconds()))
            > limits["max_data_age_seconds"]
        ):
            raise AgentError("Replay quote is stale relative to actual decision availability")
        if checked:
            gap = (item.observed_at - checked[-1].observed_at).total_seconds()
            if not 0 < gap <= max_gap_seconds:
                raise AgentError(
                    "Replay quotes must be chronological without gaps exceeding the rotation allowance"
                )
            # Model latency varies. Cadence applies to actual durable cycle
            # starts, while mark-to-market uses actual completion-time quotes.
            # Legacy diagnostics without cycle times conservatively use quotes.
            started = item.cycle_started_at or item.observed_at
            previous_start = checked[-1].cycle_started_at or checked[-1].observed_at
            if (started - previous_start).total_seconds() < min_interval_seconds:
                raise AgentError(f"Replay cycle starts must be at least {min_interval_seconds} seconds apart")
            if item.available_at <= checked[-1].available_at:
                raise AgentError("Replay decision availability is not chronological")
        checked.append(item)
    return checked, targets, limits, multiplier, asset


def _replay(observations, targets, limits, multiplier, asset, probe_config=None):
    price_bounds = limits["price_bounds_usd"][normalize_symbol(asset.symbol)]
    minimum_price = max(limits["min_price_usd"], price_bounds["min"])
    maximum_price = min(limits["max_price_usd"], price_bounds["max"])
    initial = observations[0].equity
    cash, held = initial, Decimal(0)
    peak, max_drawdown = initial, Decimal(0)
    fees, turnover = Decimal(0), Decimal(0)
    trades = round_trips = blocked_orders = below_minimum_orders = 0
    last_signal = -1
    max_trade_exposure = Decimal(0)
    day = None
    day_start = initial
    trade_log = []
    for index, quote in enumerate(observations):
        mark = (quote.bid + quote.ask) / 2
        equity = cash + held * mark
        if day != quote.observed_at.date():
            day = quote.observed_at.date()
            day_start = equity
        # The current observation may never be used to trade on its own quote.
        eligible = [
            position
            for position in range(last_signal + 1, index)
            if observations[position].available_at < quote.observed_at
        ]
        signal = None
        if eligible:
            last_signal = eligible[-1]
            signal = observations[last_signal]
        if signal is not None and signal.rating not in {"Hold", "REVIEW"}:
            current = held * mark / equity
            target = targets[signal.rating] * multiplier
            if signal.rating in {"Buy", "Overweight"}:
                if probe_config is not None:
                    target = min(
                        target, probe_position_cap(probe_config, quote, limits) * multiplier / equity
                    )
                target = max(current, target)
            elif signal.rating == "Underweight":
                target = min(current, target)
            quantity = _round(abs(equity * target / mark - held), asset.quantity_increment)
            side = "buy" if target > current else "sell"
            slip = limits["slippage_bps"] / BPS
            fee_rate = limits["fee_buffer_bps"] / BPS
            if side == "buy":
                price = _round(quote.ask * (1 + slip), asset.price_increment, ROUND_CEILING)
                affordable = cash / (price * (1 + fee_rate))
                exposure_room = (target * equity - held * price) / (price * (1 + target * fee_rate))
                quantity = _round(
                    max(Decimal(0), min(quantity, affordable, exposure_room)), asset.quantity_increment
                )
            else:
                price = _round(quote.bid * (1 - slip), asset.price_increment)
                quantity = _round(min(quantity, held), asset.quantity_increment)
            if limits.get("cap_order_to_limit", False) and price > 0:
                quantity = min(
                    quantity,
                    _round(limits["max_order_notional_usd"] / price, asset.quantity_increment),
                )
            notional = quantity * price
            fee = notional * fee_rate
            if target != current and (
                quantity < asset.min_order_size or notional < limits["min_order_notional_usd"]
            ):
                below_minimum_orders += 1
            if quantity >= asset.min_order_size and notional >= limits["min_order_notional_usd"]:
                too_large = notional > limits["max_order_notional_usd"]
                loss_stop = day_start - equity >= limits["max_daily_loss_usd"]
                exposure = (held + quantity) * price / (equity - fee) if side == "buy" else None
                # A single-symbol replay can check its own projected value only;
                # it cannot reconstruct aggregate exposure to other assets.
                projected_value = (held + quantity) * price
                exceeds_dollar_cap = side == "buy" and any(
                    projected_value > limits[name]
                    for name in ("max_position_notional_usd", "max_total_position_notional_usd")
                    if name in limits
                )
                invalid_price = not minimum_price <= price <= maximum_price
                if (
                    too_large
                    or loss_stop
                    or invalid_price
                    or exceeds_dollar_cap
                    or (side == "buy" and exposure > limits["max_position_pct"])
                ):
                    blocked_orders += 1
                else:
                    if side == "buy":
                        cash -= notional + fee
                        held += quantity
                        max_trade_exposure = max(max_trade_exposure, exposure)
                    else:
                        cash += notional - fee
                        held -= quantity
                        if held == 0:
                            round_trips += 1
                    fees += fee
                    turnover += notional
                    trades += 1
                    trade_log.append(
                        {
                            "signal_observed_at": signal.observed_at,
                            "signal_available_at": signal.available_at,
                            "hypothetical_fill_at": quote.observed_at,
                            "side": side,
                            "quantity": quantity,
                            "price": price,
                            "fee_usd": fee,
                        }
                    )
        equity = cash + held * mark
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "multiplier": multiplier,
        "initial_equity_usd": initial,
        "ending_equity_usd": equity,
        "net_pnl_usd": equity - initial,
        "fees_usd": fees,
        "turnover_usd": turnover,
        "max_drawdown_usd": max_drawdown,
        "trades": trades,
        "completed_round_trips": round_trips,
        "blocked_orders": blocked_orders,
        "below_minimum_orders": below_minimum_orders,
        "max_buy_exposure_pct": max_trade_exposure,
        "ending_cash_usd": cash,
        "ending_quantity": held,
        "trade_log": trade_log,
    }


def replay_policy(
    observations: list[Observation],
    base_targets: dict,
    risk: dict,
    multiplier: Decimal,
    *,
    asset: AssetRules,
    probe_config: dict | None = None,
    max_gap_seconds: int = MAX_GAP_SECONDS,
    min_interval_seconds: int = MIN_INTERVAL_SECONDS,
) -> dict:
    """Replay one policy for diagnostics; this function cannot recommend promotion."""
    checked, targets, limits, multiplier, asset = _inputs(
        observations,
        base_targets,
        risk,
        multiplier,
        asset,
        max_gap_seconds,
        min_interval_seconds=min_interval_seconds,
    )
    if not checked:
        raise AgentError("No observed replay data")
    return {**_replay(checked, targets, limits, multiplier, asset, probe_config), "notice": NOTICE}


def _latest_evidence(
    observations,
    base_targets,
    risk,
    current,
    asset,
    max_gap_seconds,
    *,
    min_interval_seconds=MIN_INTERVAL_SECONDS,
):
    """Validate every record, then retain the newest continuous homogeneous suffix."""
    checked = []
    suffix_start = 0
    exclusions = []
    for item in observations:
        single, _, _, _, _ = _inputs(
            [item],
            base_targets,
            risk,
            current,
            asset,
            max_gap_seconds,
            min_interval_seconds=min_interval_seconds,
        )
        item = single[0]
        if checked:
            previous = checked[-1]
            gap = (item.observed_at - previous.observed_at).total_seconds()
            if gap <= 0 or item.available_at <= previous.available_at:
                raise AgentError("Replay observations must be chronological")
            if (item.cycle_started_at is None) != (previous.cycle_started_at is None):
                raise AgentError("Replay cannot mix known and missing cycle start times")
            if (
                (item.cycle_started_at or item.observed_at)
                - (previous.cycle_started_at or previous.observed_at)
            ).total_seconds() < min_interval_seconds:
                raise AgentError(f"Replay cycle starts must be at least {min_interval_seconds} seconds apart")

            def identity(value):
                return (value.mode, value.config_digest, value.strategy_version, value.model, value.symbol)

            reasons = []
            if identity(item) != identity(previous):
                reasons.append("cohort_changed")
            if gap > max_gap_seconds:
                reasons.append("rotation_gap")
            if reasons:
                exclusions.append({"count": len(checked) - suffix_start, "reasons": reasons})
                suffix_start = len(checked)
        checked.append(item)
    return checked[suffix_start:], exclusions


def evaluate_candidates(
    observations: list[Observation],
    base_targets: dict,
    risk: dict,
    current_multiplier: Decimal = Decimal(1),
    *,
    asset: AssetRules,
    probe_config: dict | None = None,
    max_gap_seconds: int = MAX_GAP_SECONDS,
    min_interval_seconds: int = MIN_INTERVAL_SECONDS,
) -> dict:
    """Select on training data, then compare that single candidate on fresh holdout.

    The caller must persist the evaluation's observation IDs and never reuse a
    holdout window for another selection/promotion attempt. This pure function
    cannot verify database history; the orchestration layer owns that audit gate.
    """
    result = {
        "status": "insufficient_evidence",
        "notice": NOTICE,
        "minimum_observations": MIN_OBSERVATIONS,
        "holdout_evaluated": False,
        "holdout_reuse": "Caller must consume this entire window once and persist an audit record.",
    }
    try:
        suffix, exclusions = _latest_evidence(
            observations,
            base_targets,
            risk,
            current_multiplier,
            asset,
            max_gap_seconds,
            min_interval_seconds=min_interval_seconds,
        )
        checked, targets, limits, current, asset = _inputs(
            suffix,
            base_targets,
            risk,
            current_multiplier,
            asset,
            max_gap_seconds,
            min_interval_seconds=min_interval_seconds,
        )
    except (AgentError, AttributeError, TypeError, ArithmeticError, ValueError) as exc:
        result.update(
            status="invalid_evidence",
            reason=str(exc)
            if isinstance(exc, AgentError)
            else "Malformed evaluation inputs; no policy change",
        )
        return result
    result.update(
        current_multiplier=current,
        recommended_multiplier=current,
        observation_count=len(checked),
        excluded_prefix_count=sum(item["count"] for item in exclusions),
        prefix_exclusions=exclusions,
    )
    # Two-thirds of the chronological window is training; holdout is untouched
    # until exactly one candidate has been selected. No cross-boundary signal.
    split = len(checked) * 2 // 3
    training, validation = checked[:split], checked[split:]
    train_seconds = (training[-1].observed_at - training[0].observed_at).total_seconds() if training else 0
    validation_seconds = (
        (validation[-1].observed_at - validation[0].observed_at).total_seconds() if validation else 0
    )
    result.update(
        training_count=len(training),
        validation_count=len(validation),
        training_seconds=train_seconds,
        validation_seconds=validation_seconds,
        window_start=checked[0].observed_at if checked else None,
        window_end=checked[-1].observed_at if checked else None,
        validation_start=validation[0].observed_at if validation else None,
        cohort={
            "mode": checked[0].mode,
            "config_digest": checked[0].config_digest,
            "strategy_version": checked[0].strategy_version,
            "model": checked[0].model,
            "symbol": checked[0].symbol,
        }
        if checked
        else None,
    )
    ready = (
        len(checked) >= MIN_OBSERVATIONS
        and len(training) >= MIN_TRAIN_OBSERVATIONS
        and len(validation) >= MIN_VALIDATION_OBSERVATIONS
        and train_seconds >= MIN_TRAIN_SECONDS
        and validation_seconds >= MIN_VALIDATION_SECONDS
    )
    result["readiness"] = {
        "holdout_ready": ready,
        "missing_observations": max(0, MIN_OBSERVATIONS - len(checked)),
        "missing_training_observations": max(0, MIN_TRAIN_OBSERVATIONS - len(training)),
        "missing_validation_observations": max(0, MIN_VALIDATION_OBSERVATIONS - len(validation)),
        "missing_training_seconds": max(0, MIN_TRAIN_SECONDS - train_seconds),
        "missing_validation_seconds": max(0, MIN_VALIDATION_SECONDS - validation_seconds),
    }
    if not ready:
        diagnostic_prefix = training[:MIN_TRAIN_OBSERVATIONS]
        result["diagnostics"] = {
            "scope": "incumbent_training_prefix_only",
            "observation_count": len(diagnostic_prefix),
            "performance": _replay(diagnostic_prefix, targets, limits, current, asset, probe_config)
            if diagnostic_prefix
            else None,
        }
        result["reason"] = "Need >=146 observations, >=96 training spanning 16h and >=48 holdout spanning 8h"
        return result
    allowed = [value for value in CANDIDATE_MULTIPLIERS if value <= current]
    training_results = [_replay(training, targets, limits, value, asset, probe_config) for value in allowed]
    winner = max(
        training_results,
        key=lambda item: (
            item["net_pnl_usd"],
            -item["max_drawdown_usd"],
            item["multiplier"] == current,
            item["multiplier"],
        ),
    )
    candidate = winner["multiplier"]
    incumbent = _replay(validation, targets, limits, current, asset, probe_config)
    challenger = _replay(validation, targets, limits, candidate, asset, probe_config)
    result.update(
        status="no_change",
        holdout_evaluated=True,
        candidate_multiplier=candidate,
        training=training_results,
        incumbent_validation=incumbent,
        candidate_validation=challenger,
    )
    if candidate == current:
        result["reason"] = "Training did not select a different lower-exposure candidate"
        return result
    if min(incumbent["completed_round_trips"], challenger["completed_round_trips"]) < 5:
        result["reason"] = "Both holdout policies require at least five completed round trips"
        return result
    improvement = challenger["net_pnl_usd"] - incumbent["net_pnl_usd"]
    threshold = max(Decimal(1), abs(incumbent["net_pnl_usd"]) * Decimal("0.2"))
    result.update(improvement_usd=improvement, required_improvement_usd=threshold)
    if improvement < threshold:
        result["reason"] = "Holdout improvement does not clear the conservative minimum"
    elif challenger["max_drawdown_usd"] > incumbent["max_drawdown_usd"]:
        result["reason"] = "Holdout drawdown is worse than the incumbent"
    else:
        result.update(
            status="promote",
            recommended_multiplier=candidate,
            reason="Lower-exposure candidate passed training selection and fresh holdout gates",
        )
    return result
