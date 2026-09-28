"""Observational policy estimates cannot peek, invent history, or widen limits."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from crypto_agent.config import load_settings
from crypto_agent.evaluation import Observation, evaluate_candidates, replay_policy
from crypto_agent.models import AgentError, AssetRules


@pytest.fixture
def inputs():
    risk = load_settings(Path("config/demo"), "offline").risk
    risk = {**risk, "max_order_notional_usd": D(2000), "max_daily_loss_usd": D(10000)}
    targets = {"Buy": D(".1"), "Overweight": D(".07"), "Underweight": D(".03"), "Sell": D(0)}
    asset = AssetRules("BTC/USD", D(".0001"), D(".00000001"), D(".01"))
    return targets, risk, asset


def observations(count=147, rating="Hold", price=D(50000), interval_seconds=600):
    beginning = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Observation(
            observed_at=beginning + timedelta(seconds=interval_seconds * index),
            available_at=beginning + timedelta(seconds=interval_seconds * index + 5),
            rating=rating,
            bid=price,
            ask=price,
            equity=D(10000),
            config_digest="original-config",
            strategy_version="reviewed-adapter",
            model="provider:deep:quick",
            mode="paper",
            actionable=rating != "Hold",
        )
        for index in range(count)
    ]


def evaluate(data, inputs, current=D(1)):
    targets, risk, asset = inputs
    return evaluate_candidates(data, targets, risk, current, asset=asset)


def replay(data, inputs, multiplier=D(1)):
    targets, risk, asset = inputs
    return replay_policy(data, targets, risk, multiplier, asset=asset)


def test_signal_never_fills_on_its_own_quote_or_before_actual_availability(inputs):
    data = observations(3)
    data[0] = replace(data[0], rating="Buy", actionable=True)
    assert replay(data[:1], inputs)["trades"] == 0
    result = replay(data, inputs)
    assert result["trades"] == 1
    fill = result["trade_log"][0]
    assert fill["hypothetical_fill_at"] == data[1].observed_at
    assert fill["signal_available_at"] < fill["hypothetical_fill_at"]
    # A large future price change changes the later hypothetical fill, not the signal quote.
    later = [data[0], replace(data[1], bid=D(60000), ask=D(60000)), data[2]]
    later_result = replay(later, inputs)
    assert later_result["trade_log"][0]["price"] > result["trade_log"][0]["price"]


def test_signal_available_after_next_quote_cannot_be_used_at_that_quote(inputs):
    targets, risk, asset = inputs
    risk = {**risk, "max_data_age_seconds": D(1000)}
    data = observations(3)
    data[0] = replace(
        data[0], rating="Buy", actionable=True, available_at=data[1].observed_at + timedelta(seconds=1)
    )
    data[1] = replace(data[1], available_at=data[1].observed_at + timedelta(seconds=5))
    result = replay_policy(data, targets, risk, D(1), asset=asset)
    assert result["trades"] == 0  # Later Hold supersedes the delayed Buy.


def test_equal_prices_round_trip_loses_fees_spread_and_slippage(inputs):
    data = observations(3)
    data[0] = replace(data[0], rating="Buy", actionable=True)
    data[1] = replace(data[1], rating="Sell", actionable=True)
    result = replay(data, inputs)
    assert result["completed_round_trips"] == 1
    assert result["trades"] == 2
    assert result["ending_quantity"] == 0
    assert result["fees_usd"] > 0
    assert result["net_pnl_usd"] < -result["fees_usd"]
    assert "Not verified historical order fills" in result["notice"]


def test_hold_preserves_simulated_holdings_underweight_never_increases(inputs):
    data = observations(5)
    data[0] = replace(data[0], rating="Buy", actionable=True)
    data[2] = replace(data[2], rating="Underweight", actionable=True)
    result = replay(data, inputs)
    assert [trade["side"] for trade in result["trade_log"]] == ["buy", "sell"]
    assert result["completed_round_trips"] == 0
    empty = observations(2, "Underweight")
    assert replay(empty, inputs)["trades"] == 0


def test_overweight_is_a_floor_not_a_forced_reduction(inputs):
    data = observations(4, "Buy")
    data[1] = replace(data[1], rating="Overweight")
    data[2] = replace(data[2], rating="Hold", actionable=False)
    result = replay(data, inputs)
    assert all(trade["side"] == "buy" for trade in result["trade_log"])


def test_precision_cash_and_exposure_are_enforced(inputs):
    result = replay(observations(3, "Buy"), inputs)
    assert result["ending_cash_usd"] >= 0
    assert result["max_buy_exposure_pct"] <= inputs[0]["Buy"]
    for trade in result["trade_log"]:
        assert trade["quantity"] % inputs[2].quantity_increment == 0
        assert trade["price"] % inputs[2].price_increment == 0


def test_oversize_orders_are_rejected_not_split_or_clipped(inputs):
    targets, risk, asset = inputs
    risk = {**risk, "max_order_notional_usd": D(100)}
    result = replay_policy(observations(3, "Buy"), targets, risk, D(1), asset=asset)
    assert result["trades"] == 0
    assert result["blocked_orders"] == 2


def test_daily_loss_stop_blocks_subsequent_orders(inputs):
    targets, risk, asset = inputs
    risk = {**risk, "max_daily_loss_usd": D(1)}
    data = observations(4, "Buy")
    data[1] = replace(data[1], rating="Sell")
    result = replay_policy(data, targets, risk, D(1), asset=asset)
    assert result["trades"] == 1
    assert result["ending_quantity"] > 0
    assert result["blocked_orders"] >= 1


@pytest.mark.parametrize("count", [0, 20, 143, 144])
def test_insufficient_count_or_wall_clock_history_never_promotes(inputs, count):
    result = evaluate(observations(count), inputs)
    assert result["status"] == "insufficient_evidence"
    assert result["recommended_multiplier"] == 1


def test_no_trade_history_never_selects_arbitrary_best_policy(inputs):
    result = evaluate(observations(), inputs)
    assert result["status"] == "no_change"
    assert result["candidate_multiplier"] == 1
    assert result["incumbent_validation"]["trades"] == 0


def test_variable_model_latency_uses_real_cycle_cadence_and_real_quote_times(inputs):
    data = observations()
    data = [
        replace(
            item,
            cycle_started_at=item.observed_at,
            observed_at=item.observed_at + timedelta(seconds=200 if index % 2 == 0 else 100),
            available_at=item.available_at + timedelta(seconds=200 if index % 2 == 0 else 100),
        )
        for index, item in enumerate(data)
    ]
    assert (data[1].observed_at - data[0].observed_at).total_seconds() == 500
    result = evaluate(data, inputs)
    assert result["status"] == "no_change"
    assert result["window_start"] == data[0].observed_at
    assert result["window_end"] == data[-1].observed_at
    data[1] = replace(data[1], cycle_started_at=data[0].cycle_started_at + timedelta(seconds=599))
    assert evaluate(data, inputs)["status"] == "invalid_evidence"


def test_mixed_missing_cycle_times_cannot_hide_cadence_violation(inputs):
    data = observations()
    data[0] = replace(data[0], cycle_started_at=data[0].observed_at)
    result = evaluate(data, inputs)
    assert result["status"] == "invalid_evidence"
    assert "mix known and missing" in result["reason"]


@pytest.mark.parametrize("change", ["gap", "duplicate", "reverse", "stale", "crossed", "cohort"])
def test_invalid_or_discontinuous_evidence_fails_closed(inputs, change):
    data = observations()
    if change == "gap":
        data.pop(20)
        data.pop(20)
    elif change == "duplicate":
        data[20] = data[19]
    elif change == "reverse":
        data.reverse()
    elif change == "stale":
        data[20] = replace(data[20], available_at=data[20].observed_at + timedelta(hours=1))
    elif change == "crossed":
        data[20] = replace(data[20], bid=D(60000))
    else:
        data[20] = replace(data[20], model="other-provider:model")
    result = evaluate(data, inputs)
    if change in {"gap", "cohort"}:
        assert result["status"] == "insufficient_evidence"
        assert result["excluded_prefix_count"] == (20 if change == "gap" else 21)
        assert result["holdout_evaluated"] is False
    else:
        assert result["status"] == "invalid_evidence"
        assert "recommended_multiplier" not in result


def test_missing_limits_and_increased_original_targets_are_rejected(inputs):
    targets, risk, asset = inputs
    valid_risk = risk.copy()
    del risk["max_daily_loss_usd"]
    assert evaluate(observations(), inputs)["status"] == "invalid_evidence"
    with pytest.raises(AgentError, match="Missing required"):
        replay(observations(3), inputs)
    bad_targets = {**targets, "Buy": D(1)}
    assert (
        evaluate_candidates(observations(), bad_targets, valid_risk, asset=asset)["status"]
        == "invalid_evidence"
    )


def test_nonactionable_or_review_trade_cannot_enter_replay(inputs):
    data = observations()
    data[0] = replace(data[0], rating="Buy", actionable=False)
    assert evaluate(data, inputs)["status"] == "invalid_evidence"
    data[0] = replace(data[0], rating="REVIEW", actionable=False)
    assert evaluate(data, inputs)["status"] == "no_change"


def test_only_fixed_smaller_multipliers_are_eligible(inputs):
    result = evaluate(observations(), inputs, D(".75"))
    assert {item["multiplier"] for item in result["training"]} == {D(".5"), D(".75")}
    assert result["recommended_multiplier"] <= D(".75")
    result = evaluate(observations(), inputs, D("1.1"))
    assert result["status"] == "invalid_evidence"


def test_evaluation_does_not_mutate_original_targets_or_risk(inputs):
    targets, risk, _ = inputs
    original_targets, original_risk = targets.copy(), risk.copy()
    evaluate(observations(), inputs)
    assert targets == original_targets
    assert risk == original_risk


@pytest.mark.parametrize(
    "field,value",
    [
        ("config_digest", ""),
        ("model", ""),
        ("mode", "live"),
        ("equity", D(0)),
        ("bid", D("NaN")),
        ("observed_at", datetime(2026, 1, 1)),
    ],
)
def test_missing_provenance_and_malformed_observations_fail_closed(inputs, field, value):
    data = observations()
    data[0] = replace(data[0], **{field: value})
    assert evaluate(data, inputs)["status"] == "invalid_evidence"


def test_fee_losing_roundtrips_can_only_promote_reduced_exposure(inputs):
    data = observations()
    data = [
        replace(item, rating="Buy" if index % 2 == 0 else "Sell", actionable=True)
        for index, item in enumerate(data)
    ]
    result = evaluate(data, inputs)
    assert result["status"] == "promote"
    assert result["recommended_multiplier"] == D(".5")
    assert result["incumbent_validation"]["completed_round_trips"] >= 5
    assert (
        result["candidate_validation"]["max_drawdown_usd"]
        <= result["incumbent_validation"]["max_drawdown_usd"]
    )
    assert result["improvement_usd"] >= result["required_improvement_usd"]


def test_candidate_is_selected_on_training_not_holdout(inputs):
    data = observations()
    data = [
        replace(item, rating="Buy" if index % 2 == 0 else "Sell", actionable=True)
        for index, item in enumerate(data)
    ]
    first = evaluate(data, inputs)
    split = len(data) * 2 // 3
    changed_holdout = data[:split] + [replace(item, rating="Hold", actionable=False) for item in data[split:]]
    second = evaluate(changed_holdout, inputs)
    assert first["candidate_multiplier"] == second["candidate_multiplier"]
    assert first["training"] == second["training"]
    assert second["status"] == "no_change"
    assert "five completed" in second["reason"]
    assert not second["candidate_validation"]["trade_log"]


def test_holdout_starts_flat_without_training_signal_leakage(inputs):
    data = observations()
    split = len(data) * 2 // 3
    data[split - 1] = replace(data[split - 1], rating="Buy", actionable=True)
    result = evaluate(data, inputs)
    assert result["incumbent_validation"]["ending_quantity"] == 0
    assert result["incumbent_validation"]["trades"] == 0


def test_probe_replay_caps_total_exposure_and_scales_multiplier(inputs):
    targets, risk, asset = inputs
    asset = replace(asset, min_order_size=D(".000001"))
    config = {
        "intraday_entry_policy": "capped_probe",
        "intraday_probe_max_position_usd": D(10),
        "intraday_probe_cost_budget_usd": D(".15"),
    }
    data = observations(4, rating="Buy")
    for multiplier in [D(1), D(".5")]:
        result = replay_policy(data, targets, risk, multiplier, asset=asset, probe_config=config)
        assert result["trades"] == 1
        fill = result["trade_log"][0]
        assert D(0) < fill["quantity"] * fill["price"] <= D(10) * multiplier


def test_dual_symbol_rotation_accepts_22_minutes_but_not_a_missing_cycle(inputs):
    targets, risk, asset = inputs
    data = observations(3)
    start = data[0].observed_at
    data = [
        replace(
            item,
            observed_at=start + timedelta(minutes=22 * i),
            available_at=start + timedelta(minutes=22 * i, seconds=5),
        )
        for i, item in enumerate(data)
    ]
    assert replay_policy(data, targets, risk, D(1), asset=asset, max_gap_seconds=1800)["trades"] == 0
    data[-1] = replace(
        data[-1],
        observed_at=start + timedelta(minutes=70),
        available_at=start + timedelta(minutes=70, seconds=5),
    )
    with pytest.raises(AgentError, match="rotation allowance"):
        replay_policy(data, targets, risk, D(1), asset=asset, max_gap_seconds=1800)


@pytest.mark.parametrize("count", [20, 97, 145, 146])
def test_early_diagnostics_do_not_expose_or_rank_holdout(inputs, count):
    data = observations(count)
    # At 146, quote latency makes training one second shorter than 16h.
    data = [replace(item, cycle_started_at=item.observed_at) for item in data]
    data[0] = replace(data[0], observed_at=data[0].observed_at + timedelta(seconds=1))
    split = count * 2 // 3
    first = evaluate(data, inputs)
    changed = data[:split] + [
        replace(item, rating="Buy", actionable=True, bid=D(60000), ask=D(60000)) for item in data[split:]
    ]
    second = evaluate(changed, inputs)
    assert first["status"] == "insufficient_evidence"
    assert first["holdout_evaluated"] is False
    assert first["diagnostics"] == second["diagnostics"]
    assert first["diagnostics"]["observation_count"] == min(96, split)
    assert first["diagnostics"]["performance"]["multiplier"] == 1
    assert (
        not {"candidate_multiplier", "training", "incumbent_validation", "candidate_validation"}
        & first.keys()
    )


def test_146_one_second_short_preserves_evidence_without_holdout_replay(inputs):
    data = [replace(item, cycle_started_at=item.observed_at) for item in observations(146)]
    data[0] = replace(data[0], observed_at=data[0].observed_at + timedelta(seconds=1))
    result = evaluate(data, inputs)
    assert result["training_seconds"] == 16 * 3600 - 1
    assert result["readiness"]["missing_training_seconds"] == 1
    assert result["holdout_evaluated"] is False
    result = evaluate(data + observations(147)[-1:], inputs)
    # New record must have the same cycle provenance.
    assert result["status"] == "invalid_evidence"
    result = evaluate(
        data + [replace(observations(147)[-1], cycle_started_at=observations(147)[-1].observed_at)], inputs
    )
    assert result["holdout_evaluated"] is True


@pytest.mark.parametrize("boundary", ["gap", "model", "config_digest", "strategy_version"])
def test_latest_suffix_never_stitches_old_cohorts_or_gaps(inputs, boundary):
    data = observations(166)
    if boundary == "gap":
        data[20:] = [
            replace(
                item,
                observed_at=item.observed_at + timedelta(hours=2),
                available_at=item.available_at + timedelta(hours=2),
            )
            for item in data[20:]
        ]
    else:
        data[:20] = [replace(item, **{boundary: "old-cohort"}) for item in data[:20]]
    result = evaluate(data, inputs)
    assert result["observation_count"] == 146
    assert result["excluded_prefix_count"] == 20
    assert result["window_start"] == data[20].observed_at
    assert result["holdout_evaluated"] is True


def test_malformed_excluded_prefix_is_not_silently_skipped(inputs):
    data = observations(166)
    data[0] = replace(data[0], bid=D("NaN"), model="old-model")
    result = evaluate(data, inputs)
    assert result["status"] == "invalid_evidence"
    assert result["holdout_evaluated"] is False


def test_inoperative_probe_reports_minimums_without_promotion(inputs):
    targets, risk, asset = inputs
    config = {
        "intraday_entry_policy": "capped_probe",
        "intraday_probe_max_position_usd": D(10),
        "intraday_probe_cost_budget_usd": D(".15"),
    }
    data = [
        replace(item, rating="Buy" if index % 2 == 0 else "Sell", actionable=True)
        for index, item in enumerate(observations())
    ]
    result = evaluate_candidates(data, targets, risk, asset=asset, probe_config=config)
    assert result["status"] == "no_change"
    low = next(item for item in result["training"] if item["multiplier"] == D(".5"))
    assert low["trades"] == 0
    assert low["below_minimum_orders"] > 0
    assert result["recommended_multiplier"] == 1


def test_five_minute_cadence_requires_explicit_opt_in_and_full_wall_clock_history(inputs):
    targets, risk, asset = inputs
    data = observations(291, interval_seconds=300)
    with pytest.raises(AgentError, match="600 seconds"):
        replay_policy(data, targets, risk, D(1), asset=asset)
    assert evaluate_candidates(data, targets, risk, asset=asset)["status"] == "invalid_evidence"
    assert (
        replay_policy(data, targets, risk, D(1), asset=asset, min_interval_seconds=300, max_gap_seconds=600)[
            "trades"
        ]
        == 0
    )
    result = evaluate_candidates(
        data, targets, risk, asset=asset, min_interval_seconds=300, max_gap_seconds=600
    )
    assert result["status"] == "no_change"
    assert result["holdout_evaluated"] is True
    assert result["training_seconds"] >= 16 * 3600
    assert result["validation_seconds"] >= 8 * 3600


def test_faster_cadence_cannot_promote_after_only_146_observations(inputs):
    targets, risk, asset = inputs
    data = [
        replace(item, rating="Buy" if index % 2 == 0 else "Sell", actionable=True)
        for index, item in enumerate(observations(146, interval_seconds=300))
    ]
    result = evaluate_candidates(data, targets, risk, asset=asset, min_interval_seconds=300)
    assert result["status"] == "insufficient_evidence"
    assert result["minimum_observations"] == 146
    assert result["holdout_evaluated"] is False
    assert result["recommended_multiplier"] == 1
    assert result["readiness"]["missing_training_seconds"] == 8 * 3600
    assert result["readiness"]["missing_validation_seconds"] == 4 * 3600
    assert "candidate_multiplier" not in result


@pytest.mark.parametrize("interval", [True, False, 59, 3601, 300.0, "300", None])
def test_malformed_cadence_fails_closed_including_empty_evidence(inputs, interval):
    targets, risk, asset = inputs
    with pytest.raises(AgentError, match="minimum interval"):
        replay_policy([], targets, risk, D(1), asset=asset, min_interval_seconds=interval)
    for data in [[], observations(2)]:
        result = evaluate_candidates(data, targets, risk, asset=asset, min_interval_seconds=interval)
        assert result["status"] == "invalid_evidence"
        assert result["holdout_evaluated"] is False


@pytest.mark.parametrize("interval,gap", [(60, 60), (300, 300), (300, 600), (3600, 14400)])
def test_valid_cadence_and_gap_bounds(inputs, interval, gap):
    targets, risk, asset = inputs
    result = replay_policy(
        observations(2, interval_seconds=interval),
        targets,
        risk,
        D(1),
        asset=asset,
        min_interval_seconds=interval,
        max_gap_seconds=gap,
    )
    assert result["trades"] == 0


@pytest.mark.parametrize("interval,gap", [(300, 299), (300, 2401), (3600, 14401), (300, True)])
def test_invalid_gap_allowance_fails_closed(inputs, interval, gap):
    targets, risk, asset = inputs
    result = evaluate_candidates(
        observations(2),
        targets,
        risk,
        asset=asset,
        min_interval_seconds=interval,
        max_gap_seconds=gap,
    )
    assert result["status"] == "invalid_evidence"
    assert "gap allowance" in result["reason"]


def test_explicit_faster_cadence_still_checks_real_cycle_starts_before_excluding_prefix(inputs):
    targets, risk, asset = inputs
    data = [
        replace(item, cycle_started_at=item.observed_at) for item in observations(3, interval_seconds=300)
    ]
    data[1] = replace(data[1], cycle_started_at=data[0].cycle_started_at + timedelta(seconds=299))
    with pytest.raises(AgentError, match="300 seconds"):
        replay_policy(data, targets, risk, D(1), asset=asset, min_interval_seconds=300)
    data[2] = replace(data[2], config_digest="new-cohort")
    result = evaluate_candidates(data, targets, risk, asset=asset, min_interval_seconds=300)
    assert result["status"] == "invalid_evidence"
    assert "300 seconds" in result["reason"]


def test_opt_in_order_cap_steps_buys_and_sells_without_exceeding_limits(inputs):
    targets, risk, asset = inputs
    risk = {**risk, "max_order_notional_usd": D(100), "cap_order_to_limit": True}
    data = observations(10, rating="Buy")
    data[4:] = [replace(item, rating="Sell") for item in data[4:]]
    result = replay_policy(data, targets, risk, D(1), asset=asset)
    buys = [trade for trade in result["trade_log"] if trade["side"] == "buy"]
    sells = [trade for trade in result["trade_log"] if trade["side"] == "sell"]
    assert len(buys) > 1
    assert len(sells) > 1
    assert result["blocked_orders"] == 0
    assert result["ending_cash_usd"] >= 0
    assert result["ending_quantity"] >= 0
    assert result["max_buy_exposure_pct"] <= targets["Buy"]
    assert sum(trade["quantity"] for trade in sells) <= sum(trade["quantity"] for trade in buys)
    for trade in result["trade_log"]:
        assert D(0) < trade["quantity"] * trade["price"] <= D(100)
        assert trade["quantity"] % asset.quantity_increment == 0


def test_explicit_false_order_cap_preserves_oversize_rejection(inputs):
    targets, risk, asset = inputs
    risk = {**risk, "max_order_notional_usd": D(100)}
    data = observations(3, "Buy")
    implicit = replay_policy(data, targets, risk, D(1), asset=asset)
    explicit = replay_policy(data, targets, {**risk, "cap_order_to_limit": False}, D(1), asset=asset)
    assert implicit == explicit
    assert explicit["trades"] == 0
    assert explicit["blocked_orders"] == 2


def test_opt_in_order_cap_preserves_broker_minimum(inputs):
    targets, risk, asset = inputs
    risk = {**risk, "max_order_notional_usd": D(100), "cap_order_to_limit": True}
    asset = replace(asset, min_order_size=D(".01"))
    result = replay_policy(observations(3, "Buy"), targets, risk, D(1), asset=asset)
    assert result["trades"] == 0
    assert result["below_minimum_orders"] == 2


@pytest.mark.parametrize("cap", ["max_position_notional_usd", "max_total_position_notional_usd"])
@pytest.mark.parametrize("clip_orders", [False, True])
def test_high_equity_cannot_bypass_optional_dollar_caps(inputs, cap, clip_orders):
    targets, risk, asset = inputs
    risk = {
        **risk,
        cap: D(5000),
        "max_order_notional_usd": D(500) if clip_orders else D(200000),
        "cap_order_to_limit": clip_orders,
    }
    data = [replace(item, equity=D(1000000)) for item in observations(20, "Buy")]
    result = replay_policy(data, targets, risk, D(1), asset=asset)
    assert result["blocked_orders"] > 0
    assert result["ending_quantity"] * data[-1].ask <= D(5000)
    held = D(0)
    for trade in result["trade_log"]:
        assert trade["side"] == "buy"
        held += trade["quantity"]
        assert held * trade["price"] <= D(5000)
    if clip_orders:
        assert result["trades"] > 0
    else:
        assert result["trades"] == 0


@pytest.mark.parametrize("cap", ["max_position_notional_usd", "max_total_position_notional_usd"])
def test_replay_can_sell_appreciated_position_above_dollar_cap(inputs, cap):
    targets, risk, asset = inputs
    risk = {**risk, cap: D(5000), "max_order_notional_usd": D(500), "cap_order_to_limit": True}
    data = [replace(item, equity=D(1000000)) for item in observations(17, "Buy")]
    data[14] = replace(data[14], rating="Sell")
    data[15:] = [replace(item, rating="Sell", bid=D(60000), ask=D(60000)) for item in data[15:]]
    result = replay_policy(data, targets, risk, D(1), asset=asset)
    buys = [trade for trade in result["trade_log"] if trade["side"] == "buy"]
    sells = [trade for trade in result["trade_log"] if trade["side"] == "sell"]
    held_before_sell = sum(trade["quantity"] for trade in buys)
    assert sells
    assert held_before_sell * sells[0]["price"] > D(5000)
    assert D(0) <= result["ending_quantity"] < held_before_sell


def test_replay_blocks_adjusted_xrp_buy_price_above_symbol_maximum(inputs):
    targets, risk, _ = inputs
    risk = {
        **risk,
        "allowed_symbols": ["XRP/USD"],
        "price_bounds_usd": {"XRP/USD": {"min": D(".01"), "max": D(1000)}},
        "min_price_usd": D(".01"),
    }
    asset = AssetRules("XRP/USD", D(".01"), D(".01"), D(".0001"))
    data = [replace(item, symbol="XRP/USD") for item in observations(2, "Buy", price=D(1000))]
    result = replay_policy(data, targets, risk, D(1), asset=asset)
    assert result["trades"] == 0
    assert result["blocked_orders"] == 1


def test_replay_blocks_adjusted_btc_sell_price_below_symbol_minimum(inputs):
    targets, risk, asset = inputs
    risk = {**risk, "min_price_usd": D(".01")}
    data = observations(3, "Buy", price=D(100))
    data[1] = replace(data[1], rating="Sell")
    result = replay_policy(data, targets, risk, D(1), asset=asset)
    assert [trade["side"] for trade in result["trade_log"]] == ["buy"]
    assert result["ending_quantity"] > 0
    assert result["blocked_orders"] == 1
