"""Fast, fail-closed AI rating over validated one-minute crypto features."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from crypto_agent.config import validate_intraday_policy
from crypto_agent.data.features import IntradayFeatures, build_intraday_features
from crypto_agent.models import (
    AgentError,
    MarketSnapshot,
    PortfolioSnapshot,
    PriceBar,
    TradeDecision,
    decimal,
    dumps,
    normalize_symbol,
    timestamp,
    utcnow,
)
from crypto_agent.risk.intraday import probe_position_cap, round_trip_cost_bps

RATINGS = frozenset({"Buy", "Overweight", "Hold", "Underweight", "Sell"})
OUTPUT_RATINGS = RATINGS | {"REVIEW"}
_OUTPUT_FIELDS = {"rating", "summary", "evidence"}
_RUNTIME_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR"}


class IntradayAIStrategy:
    """Use AI for direction; keep sizing and admissibility deterministic."""

    version = "intraday-ai-1min-v3.1-capped-probe"
    requires_intraday_bars = True

    def __init__(self, config: dict, risk: dict):
        self.config = dict(config)
        if config.get("intraday_probe_profile") == "expanded_paper":
            self.version = "intraday-ai-1min-v4.1-expanded-paper"
        if config.get("intraday_trade_filter", "none") == "confirm2_cooldown30":
            self.version = "intraday-ai-1min-v5.1-low-turnover-paper"
        if config.get("asset_type") != "crypto" or config.get("decision_profile") != "intraday_10m":
            raise AgentError("Intraday AI requires crypto mode and intraday_10m profile")
        if config.get("llm_provider") != "openai" or not config.get("quick_think_llm"):
            raise AgentError("Intraday AI currently requires an OpenAI-compatible model")
        for name in (
            "timeout_seconds",
            "decision_ttl_seconds",
            "intraday_lookback_bars",
            "intraday_min_bars",
            "intraday_max_bar_age_seconds",
            "intraday_entry_score",
        ):
            if type(config.get(name)) is not int:
                raise AgentError(f"Invalid intraday integer setting: {name}")
        if not 31 <= config["intraday_min_bars"] <= config["intraday_lookback_bars"] <= 240:
            raise AgentError("Invalid intraday lookback/minimum bar count")
        if not 1 <= config["intraday_max_bar_age_seconds"] <= 600:
            raise AgentError("Invalid intraday bar freshness limit")
        if not 1 <= config["intraday_entry_score"] <= 4:
            raise AgentError("Intraday entry score must be in [1, 4]")
        self.entry_policy = validate_intraday_policy(config)
        self.momentum_threshold = decimal(
            config.get("intraday_momentum_threshold_bps"), "intraday momentum threshold"
        )
        if not Decimal("0.1") <= self.momentum_threshold <= Decimal(100):
            raise AgentError("Intraday momentum threshold must be in [0.1, 100] bps")
        targets = config.get("rating_target_pct")
        if not isinstance(targets, dict) or set(targets) != RATINGS - {"Hold"}:
            raise AgentError("All four actionable rating targets must be configured")
        self.targets = {key: decimal(value, "rating target") for key, value in targets.items()}
        if not (
            self.targets["Sell"]
            == 0
            <= self.targets["Underweight"]
            <= self.targets["Overweight"]
            <= self.targets["Buy"]
            <= 1
        ):
            raise AgentError("Rating targets must be ordered with Sell equal to zero")
        if not isinstance(risk, dict):
            raise AgentError("Intraday AI requires validated risk cost settings")
        self.risk = dict(risk)
        self.fee_buffer_bps = decimal(risk.get("fee_buffer_bps"), "fee buffer")
        self.slippage_bps = decimal(risk.get("slippage_bps"), "slippage buffer")
        if self.fee_buffer_bps <= 0 or self.slippage_bps <= 0:
            raise AgentError("Intraday cost buffers must be positive")
        self.model = "openai:" + config["quick_think_llm"]

    def _entry_costs(self, market: MarketSnapshot, features: IntradayFeatures) -> tuple[Decimal, Decimal]:
        cost_bps = round_trip_cost_bps(market, self.risk)
        gross_move_bps = max(
            Decimal(0),
            features.return_3m_bps,
            features.return_10m_bps,
            features.return_30m_bps,
        )
        return gross_move_bps, cost_bps

    @property
    def bar_request(self) -> dict:
        return {
            "timeframe": "1Min",
            "limit": self.config["intraday_lookback_bars"] + 1,
        }

    def _decision(
        self,
        started_at: datetime,
        symbol: str,
        reason: str,
        *,
        rating: str = "REVIEW",
        target: Decimal | None = None,
        evidence: tuple[str, ...] = (),
        actionable: bool = False,
        evaluation_eligible: bool = False,
    ) -> TradeDecision:
        return TradeDecision(
            normalize_symbol(symbol),
            target,
            reason,
            started_at + timedelta(seconds=self.config["decision_ttl_seconds"]),
            started_at,
            rating,
            self.version,
            self.model,
            evidence,
            actionable,
            evaluation_eligible,
        )

    def _features(
        self, market: MarketSnapshot, bars: tuple[PriceBar, ...], *, now: datetime | None = None
    ) -> IntradayFeatures:
        return build_intraday_features(
            market,
            bars,
            lookback_bars=self.config["intraday_lookback_bars"],
            min_bars=self.config["intraday_min_bars"],
            max_age_seconds=self.config["intraday_max_bar_age_seconds"],
            momentum_threshold_bps=self.momentum_threshold,
            now=now,
        )

    def parse_output(
        self,
        output: object,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        features: IntradayFeatures,
        started_at: datetime,
        *,
        now: datetime | None = None,
    ) -> TradeDecision:
        now = timestamp(now or utcnow())
        if now >= started_at + timedelta(seconds=self.config["decision_ttl_seconds"]):
            return self._decision(started_at, market.symbol, "Intraday decision expired during analysis")
        if not isinstance(output, dict) or set(output) != _OUTPUT_FIELDS:
            return self._decision(started_at, market.symbol, "Intraday model output schema is invalid")
        rating, summary, evidence = output.get("rating"), output.get("summary"), output.get("evidence")
        if (
            rating not in OUTPUT_RATINGS
            or not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > 1000
            or not isinstance(evidence, list)
            or not 2 <= len(evidence) <= 5
            or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in evidence)
        ):
            return self._decision(started_at, market.symbol, "Intraday model output values are invalid")
        if rating == "REVIEW":
            return self._decision(
                started_at,
                market.symbol,
                "Model requested REVIEW: " + summary.strip(),
                evidence=tuple(item.strip() for item in evidence),
                evaluation_eligible=True,
            )
        if portfolio.equity_usd <= 0 or market.price <= 0:
            return self._decision(started_at, market.symbol, "Positive equity and quote are required")
        current = portfolio.quantity_for(market.symbol) * market.price / portfolio.equity_usd
        if not 0 <= current <= 1:
            return self._decision(started_at, market.symbol, "Existing allocation is outside spot bounds")
        target = current if rating == "Hold" else self.targets[rating]
        if rating in {"Buy", "Overweight"}:
            if self.entry_policy == "capped_probe":
                target = min(
                    target, probe_position_cap(self.config, market, self.risk) / portfolio.equity_usd
                )
            target = max(target, current)
            minimum_score = max(self.config["intraday_entry_score"], 3 if rating == "Buy" else 1)
            if target > current and features.momentum_score < minimum_score:
                return self._decision(
                    started_at,
                    market.symbol,
                    "Long-entry rating lacks the configured quantitative momentum confirmation",
                    evidence=(f"Momentum score {features.momentum_score}; required {minimum_score}",),
                    evaluation_eligible=True,
                )
            gross_move_bps, round_trip_cost_bps = self._entry_costs(market, features)
            if (
                self.entry_policy == "cost_cover"
                and target > current
                and gross_move_bps < round_trip_cost_bps
            ):
                return self._decision(
                    started_at,
                    market.symbol,
                    "Long-entry move proxy does not cover configured spread, fee and slippage buffers",
                    evidence=(
                        f"Gross directional move proxy {gross_move_bps} bps; required round-trip cost "
                        f"{round_trip_cost_bps} bps",
                    ),
                    evaluation_eligible=True,
                )
        elif rating == "Underweight":
            target = min(target, current)
            maximum_score = -self.config["intraday_entry_score"]
            if target < current and features.momentum_score > maximum_score:
                return self._decision(
                    started_at,
                    market.symbol,
                    "Underweight rating lacks symmetric negative momentum confirmation",
                    evidence=(f"Momentum score {features.momentum_score}; required at most {maximum_score}",),
                    evaluation_eligible=True,
                )
        elif rating == "Sell" and target < current:
            maximum_score = -max(self.config["intraday_entry_score"], 3)
            if features.momentum_score > maximum_score:
                return self._decision(
                    started_at,
                    market.symbol,
                    "Sell rating lacks strong negative momentum confirmation",
                    evidence=(f"Momentum score {features.momentum_score}; required at most {maximum_score}",),
                    evaluation_eligible=True,
                )
        gross_move_bps, round_trip_cost_bps = self._entry_costs(market, features)
        feature_evidence = (
            f"Closed 1-minute bars {features.bar_count}, last start {features.last_bar_at.isoformat()}",
            f"Returns bps 3m={features.return_3m_bps}, 10m={features.return_10m_bps}, "
            f"30m={features.return_30m_bps}; EMA5/20={features.ema_5_vs_20_bps}; score={features.momentum_score}",
            f"Quote {market.price} at {market.observed_at.isoformat()}; current allocation {current}; target {target}",
            f"Gross directional move proxy {gross_move_bps} bps; configured round-trip cost "
            f"{round_trip_cost_bps} bps",
            f"Entry policy {self.entry_policy}; target notional {target * portfolio.equity_usd} USD; "
            "cost reserve is not predicted profit",
        )
        return self._decision(
            started_at,
            market.symbol,
            summary.strip(),
            rating=rating,
            target=target,
            evidence=tuple(item.strip() for item in evidence) + feature_evidence,
            actionable=rating != "Hold",
            evaluation_eligible=True,
        )

    def decide(
        self,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        bars: tuple[PriceBar, ...],
    ) -> TradeDecision:
        started_at = utcnow()
        normalize_symbol(market.symbol)
        if portfolio.open_orders:
            return self._decision(
                started_at, market.symbol, "Open orders must be reconciled before intraday analysis"
            )
        try:
            features = self._features(market, bars, now=started_at)
        except AgentError as exc:
            return self._decision(started_at, market.symbol, str(exc))
        payload = {
            "model": self.config["quick_think_llm"],
            "backend_url": self.config.get("backend_url"),
            "request_timeout_seconds": self.config["timeout_seconds"],
            "context": {
                "features": features.payload(),
                "account": {
                    "equity_usd": portfolio.equity_usd,
                    "cash_usd": portfolio.cash_usd,
                    "current_quantity": portfolio.quantity_for(market.symbol),
                    "current_allocation": portfolio.quantity_for(market.symbol)
                    * market.price
                    / portfolio.equity_usd,
                },
                "quote": {
                    "bid": market.bid,
                    "ask": market.ask,
                    "observed_at": market.observed_at,
                    "source": market.source,
                },
                "policy": {
                    "entry_score": self.config["intraday_entry_score"],
                    "underweight_score_at_most": -self.config["intraday_entry_score"],
                    "sell_score_at_most": -max(self.config["intraday_entry_score"], 3),
                    "entry_policy": self.entry_policy,
                    "round_trip_cost_reserve_bps": round_trip_cost_bps(market, self.risk),
                    "probe_position_cap_usd": probe_position_cap(self.config, market, self.risk)
                    if self.entry_policy == "capped_probe"
                    else None,
                    "probe_cost_budget_usd": self.config.get("intraday_probe_cost_budget_usd"),
                    "rating_targets": self.targets,
                    "long_only": True,
                    "leverage": False,
                    "horizon_minutes": "10-180",
                },
            },
        }
        env = {key: value for key, value in os.environ.items() if key in _RUNTIME_ENV}
        if os.environ.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1")
        worker = Path(__file__).with_name("_intraday_ai_worker.py")
        try:
            with TemporaryDirectory(prefix="crypto-agent-intraday-") as directory:
                result = subprocess.run(
                    [sys.executable, str(worker)],
                    input=dumps(payload),
                    capture_output=True,
                    text=True,
                    timeout=self.config["timeout_seconds"],
                    cwd=directory,
                    env=env,
                    check=False,
                )
            if result.returncode != 0:
                return self._decision(
                    started_at, market.symbol, "Intraday AI worker failed; no order is allowed"
                )
            output = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            return self._decision(started_at, market.symbol, "Intraday AI timed out; no order is allowed")
        except (OSError, ValueError):
            return self._decision(
                started_at, market.symbol, "Intraday AI response is unavailable or malformed"
            )
        return self.parse_output(output, market, portfolio, features, started_at)
