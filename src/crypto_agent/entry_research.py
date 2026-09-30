"""Read-only, training-only comparisons of tighter entry filters.

This module is not registered as a trading strategy. It never calls a broker or
model, recommends deployment, or uses the withheld final third for selection.
"""

import argparse
import json
import sqlite3
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from crypto_agent.data.features import build_intraday_features
from crypto_agent.evaluation import Observation, replay_policy
from crypto_agent.models import AgentError, AssetRules, MarketSnapshot, PriceBar, decimal, dumps, timestamp

COHORT_VERSIONS = {
    "intraday-ai-1min-v3-capped-probe",
    "intraday-ai-1min-v5.1-low-turnover-paper",
    "intraday-ai-1min-v5.2-economic-low-turnover-paper",
}
VARIANTS = (
    "incumbent",
    "entry_score_3",
    "aligned_short_trend",
    "cost_cover",
    "cooldown30",
    "cost_cover_cooldown30",
)


def _replay_cadence(config):
    paper = config.get("paper", {})
    interval = paper.get("automatic_interval_seconds", 600)
    symbols_per_cycle = paper.get("automatic_symbols_per_cycle", 1)
    if (
        type(interval) is not int
        or not 60 <= interval <= 3600
        or type(symbols_per_cycle) is not int
        or not 1 <= symbols_per_cycle <= 3
    ):
        raise AgentError("Research requires valid automatic Paper cadence")
    return interval, interval * symbols_per_cycle


def _cost_cover(observation, features, risk):
    if risk is None:
        raise AgentError("Entry research cost_cover requires risk buffers")
    bid, ask = decimal(observation.bid), decimal(observation.ask)
    if not 0 < bid <= ask:
        raise AgentError("Entry research requires a valid saved quote")
    spread_bps = (ask - bid) / ((ask + bid) / 2) * Decimal(10000)
    cost_bps = spread_bps + 2 * (decimal(risk["fee_buffer_bps"]) + decimal(risk["slippage_bps"]))
    gross_move_bps = max(
        Decimal(0),
        features.return_3m_bps,
        features.return_10m_bps,
        features.return_30m_bps,
    )
    return gross_move_bps >= cost_bps


def filtered_observation(
    observation, features, variant, *, risk=None, last_entry_at=None, cooldown_seconds=1800
):
    """Only suppress existing Buy/Overweight signals; never invent a trade."""
    if variant not in VARIANTS:
        raise AgentError("Unknown research entry filter")
    if variant == "incumbent" or observation.rating not in {"Buy", "Overweight"}:
        return observation
    if features is None:
        raise AgentError("Entry research requires original validated features")
    passes = True
    if variant == "entry_score_3":
        passes = features.momentum_score >= 3
    elif variant == "aligned_short_trend":
        passes = (
            features.return_3m_bps >= Decimal(2)
            and features.return_10m_bps >= Decimal(3)
            and features.ema_5_vs_20_bps >= Decimal("1.5")
        )
    elif variant in {"cost_cover", "cost_cover_cooldown30"}:
        passes = _cost_cover(observation, features, risk)
    if passes and variant in {"cooldown30", "cost_cover_cooldown30"} and last_entry_at is not None:
        passes = (observation.available_at - last_entry_at).total_seconds() >= cooldown_seconds
    return observation if passes else replace(observation, rating="REVIEW", actionable=False)


def _observation(body):
    values = dict(body)
    for field in ("observed_at", "available_at", "cycle_started_at"):
        if values.get(field) is not None:
            values[field] = timestamp(values[field])
    for field in ("bid", "ask", "equity"):
        values[field] = Decimal(values[field])
    return Observation(**values)


def _features(row, strategy):
    raw = json.loads(row["market_json"])
    market = MarketSnapshot(
        **{
            **raw,
            "observed_at": timestamp(raw["observed_at"]),
            **{name: Decimal(raw[name]) for name in ("price", "bid", "ask")},
        }
    )
    bars = tuple(
        PriceBar(
            **{
                **bar,
                "observed_at": timestamp(bar["observed_at"]),
                **{name: Decimal(bar[name]) for name in ("open", "high", "low", "close", "volume")},
            }
        )
        for bar in json.loads(row["bars"])
    )
    decision = json.loads(row["decision"])
    return build_intraday_features(
        market,
        bars,
        lookback_bars=strategy["intraday_lookback_bars"],
        min_bars=strategy["intraday_min_bars"],
        max_age_seconds=strategy["intraday_max_bar_age_seconds"],
        momentum_threshold_bps=Decimal(strategy["intraday_momentum_threshold_bps"]),
        now=timestamp(decision["created_at"]),
    )


def compare(snapshot: Path, asset_file: Path):
    """Open the snapshot read-only. Same-cohort valid entries only; no gap stitching."""
    connection = sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        state = json.loads(
            connection.execute("SELECT value FROM metadata WHERE key='automatic_policy'").fetchone()[0]
        )
        cohort_digest = state["approval_digest"]
        assets = json.loads(asset_file.read_text())["assets"]
        all_rows = connection.execute("""
            SELECT o.id,o.run_id,o.body,r.market_json,r.config_digest,r.config_json,
                   d.body AS decision,c.body AS bars
            FROM strategy_observations o JOIN runs r ON r.id=o.run_id
            JOIN decisions d ON d.run_id=o.run_id
            LEFT JOIN intraday_contexts c ON c.run_id=o.run_id ORDER BY o.id
        """).fetchall()
        eligible = []
        for row in all_rows:
            item = json.loads(row["body"])
            if (
                item.get("config_digest") == cohort_digest
                and item.get("symbol") in {"BTC/USD", "XRP/USD"}
                and item.get("strategy_version") in COHORT_VERSIONS
                and item.get("mode") == "paper"
            ):
                eligible.append(row)
        if not eligible:
            raise AgentError("No saved observations for the approved research cohort")
        # The effective run digest legitimately differs from the approval digest
        # after target normalization. Bind to associated run settings, never an
        # unrelated later run or today's settings. Mixed effective policies need
        # separate research datasets, even under one original approval.
        signatures = {(row["config_digest"], dumps(json.loads(row["config_json"]))) for row in eligible}
        if len(signatures) != 1:
            raise AgentError("Research cannot mix effective run configurations")
        config = json.loads(eligible[-1]["config_json"])
        min_interval_seconds, max_gap_seconds = _replay_cadence(config)
        strategy = config["strategy"]
        if any(
            json.loads(row["body"]).get("model") != "openai:" + strategy["quick_think_llm"]
            for row in eligible
        ):
            raise AgentError("Research model provenance differs from associated run settings")
        output = {
            "mode": "offline_research",
            "deployment_recommended": False,
            "holdout_evaluated": False,
            "notice": "Exploratory training-only replay of saved Paper observations; not actual fills or proof of profitability. Each contiguous segment starts flat. Repeated training comparisons cannot establish independent validation.",
            "variants": {
                "incumbent": "Recorded AI ratings unchanged",
                "entry_score_3": "Suppress recorded entries with momentum score below 3",
                "aligned_short_trend": "Suppress entries unless 3m >=2bps, 10m >=3bps and EMA5/20 >=1.5bps",
                "cost_cover": "Suppress entries whose saved quote spread, fee and slippage buffers exceed the original move proxy",
                "cooldown30": "Suppress entries less than 30 minutes after a prior retained training entry",
                "cost_cover_cooldown30": "Apply both training-only cost_cover and cooldown30 filters",
            },
            "config_digest": cohort_digest,
            "effective_run_config_digest": eligible[-1]["config_digest"],
            "replay_min_interval_seconds": min_interval_seconds,
            "replay_max_gap_seconds": max_gap_seconds,
            "symbols": {},
        }
        for symbol in ("BTC/USD", "XRP/USD"):
            selected = []
            for row in eligible:
                item = json.loads(row["body"])
                if (
                    item.get("config_digest") == cohort_digest
                    and item.get("symbol") == symbol
                    and item.get("strategy_version") in COHORT_VERSIONS
                    and item.get("mode") == "paper"
                    and item.get("model") == "openai:" + strategy["quick_think_llm"]
                ):
                    selected.append(row)
            split = len(selected) * 2 // 3
            training = selected[:split]
            # Never compute features or replay outcomes from the final third.
            segments, segment, excluded = [], [], []
            for row in training:
                observation = _observation(json.loads(row["body"]))
                try:
                    features = (
                        _features(row, strategy) if observation.rating in {"Buy", "Overweight"} else None
                    )
                except (AgentError, TypeError, KeyError, ValueError) as exc:
                    if segment:
                        segments.append(segment)
                        segment = []
                    excluded.append({"id": row["id"], "reason": str(exc)})
                    continue
                if (
                    segment
                    and (observation.observed_at - segment[-1][0].observed_at).total_seconds()
                    > max_gap_seconds
                ):
                    segments.append(segment)
                    segment = []
                segment.append((observation, features))
            if segment:
                segments.append(segment)
            raw_asset = assets[symbol]
            asset = AssetRules(
                **{
                    **raw_asset,
                    **{
                        name: Decimal(raw_asset[name])
                        for name in ("min_order_size", "quantity_increment", "price_increment")
                    },
                }
            )
            comparisons = {}
            for variant in VARIANTS:
                replays = []
                suppressed = 0
                for part in segments:
                    filtered = []
                    last_entry_at = None
                    for observation, features in part:
                        current = filtered_observation(
                            observation,
                            features,
                            variant,
                            risk=config["risk"],
                            last_entry_at=last_entry_at,
                        )
                        filtered.append(current)
                        if current.rating in {"Buy", "Overweight"} and current.actionable:
                            last_entry_at = current.available_at
                    suppressed += sum(
                        new.rating != old.rating for new, (old, _) in zip(filtered, part, strict=True)
                    )
                    result = replay_policy(
                        filtered,
                        strategy["rating_target_pct"],
                        config["risk"],
                        Decimal(1),
                        asset=asset,
                        probe_config=strategy,
                        max_gap_seconds=max_gap_seconds,
                        min_interval_seconds=min_interval_seconds,
                    )
                    replays.append(
                        {
                            "start": filtered[0].observed_at,
                            "end": filtered[-1].observed_at,
                            "observations": len(filtered),
                            **{
                                key: result[key]
                                for key in (
                                    "net_pnl_usd",
                                    "fees_usd",
                                    "max_drawdown_usd",
                                    "trades",
                                    "completed_round_trips",
                                    "ending_quantity",
                                )
                            },
                        }
                    )
                comparisons[variant] = {
                    "suppressed_entry_signals": suppressed,
                    "segment_net_pnl_sum_usd": sum((r["net_pnl_usd"] for r in replays), Decimal(0)),
                    "trades": sum(r["trades"] for r in replays),
                    "completed_round_trips": sum(r["completed_round_trips"] for r in replays),
                    "segments": replays,
                }
            output["symbols"][symbol] = {
                "total_observations": len(selected),
                "training_observations": len(training),
                "withheld_observations": len(selected) - split,
                "training_ratings": dict(Counter(json.loads(row["body"])["rating"] for row in training)),
                "excluded_training_observations": excluded,
                "segment_count": len(segments),
                "comparisons": comparisons,
            }
        return output
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--asset-rules", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {args.snapshot.resolve(), args.asset_rules.resolve()}:
        parser.error("Output must not overwrite the snapshot or asset metadata")
    report = compare(args.snapshot, args.asset_rules)
    args.output.write_text(dumps(report) + "\n")
    print(
        dumps(
            {
                symbol: {key: value for key, value in body.items() if key != "comparisons"}
                for symbol, body in report["symbols"].items()
            }
        )
    )


if __name__ == "__main__":
    main()
