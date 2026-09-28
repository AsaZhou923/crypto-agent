"""Fail-closed adapter for the pinned TradingAgents v0.5.0 source checkout.

The upstream parser permits prose heuristics. We require the explicit rendered
PortfolioDecision schema. Inference runs in a killable process with a deadline.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from crypto_agent.models import (
    UPSTREAM_COMMIT,
    AgentError,
    MarketSnapshot,
    PortfolioSnapshot,
    TradeDecision,
    decimal,
    dumps,
    normalize_symbol,
    timestamp,
    upstream_symbol,
    utcnow,
)

RATINGS = frozenset({"Buy", "Overweight", "Hold", "Underweight", "Sell"})
DECISION_PROFILES = {
    "balanced": (
        "Decision profile: balanced. Weigh the available evidence across the full reported horizon "
        "and do not force an entry. Code-enforced rating targets and risk limits control sizing."
    ),
    "short_term_small": (
        "Decision profile: short-term small Paper probe. Evaluate a one-to-three-day holding horizon "
        "and prioritize current price structure, momentum, volatility, spread, and clearly identified "
        "entry invalidation levels. When the account is flat, a favorable but not fully confirmed "
        "short-term setup may be rated Overweight for a small probe; reserve Buy for strong confirming "
        "evidence. Do not default to Sell solely because long-horizon fundamentals or unrelated data are "
        "absent, but do not force an entry, invent evidence, or weaken REVIEW behavior. Underweight and "
        "Sell cannot open a long position, and short selling remains forbidden. Code-enforced rating "
        "targets and risk limits control sizing; ignore any conflicting sizing suggestion in model prose."
    ),
}
_FIELDS = ("Rating", "Executive Summary", "Investment Thesis", "Price Target", "Time Horizon")
_FIELD = re.compile(r"^\s*(?:#{1,6}\s+)?(?:\*\*)?(" + "|".join(_FIELDS) + r")(?:\*\*)?\s*:\s*(.*?)\s*$")
_MISSING = frozenset({"", "n/a", "none", "not provided", "unavailable", "no data", "null"})

# Brokerage credentials, unrelated secrets, trace exporters and PYTHONPATH do
# not cross into the model process. Only local model and runtime settings do.
_MODEL_ENV = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_VERSION",
    "OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT_NAME",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "MOONSHOT_API_KEY",
    "GROQ_API_KEY",
    "NVIDIA_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "OLLAMA_BASE_URL",
    "TRADINGAGENTS_LLM_BACKEND_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
}
_RUNTIME_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR"}
_GRAPH_CONFIG = {
    "llm_provider",
    "backend_url",
    "deep_think_llm",
    "quick_think_llm",
    "max_debate_rounds",
    "max_risk_discuss_rounds",
    "output_language",
}


def verify_upstream(repository: str | Path, expected_commit: str = UPSTREAM_COMMIT) -> None:
    """Require the exact reviewed revision and clean source, without changing it."""
    if expected_commit != UPSTREAM_COMMIT:
        raise AgentError("TradingAgents commit does not match the reviewed adapter revision")
    repository = Path(repository).resolve()
    if not (repository / "tradingagents/graph/trading_graph.py").is_file():
        raise AgentError("TradingAgents source checkout is missing")
    candidates = [shutil.which("git"), "/opt/homebrew/bin/git", "/usr/local/bin/git"]
    for binary in dict.fromkeys(path for path in candidates if path):
        try:
            revision = subprocess.run(
                [binary, "-C", str(repository), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            dirty = subprocess.run(
                [
                    binary,
                    "-C",
                    str(repository),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=normal",
                    "--",
                    "tradingagents",
                    "pyproject.toml",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if revision != expected_commit or dirty:
            raise AgentError("TradingAgents checkout must be clean and at the pinned commit")
        return
    raise AgentError("Cannot verify TradingAgents commit; install a working git executable")


def _sections(report: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current = None
    for line in unicodedata.normalize("NFKC", report).splitlines():
        match = _FIELD.fullmatch(line)
        if match:
            current, value = match.groups()
            if current in fields:
                raise AgentError("Ambiguous repeated decision fields")
            fields[current] = value
        elif current:
            fields[current] += "\n" + line
        elif line.strip():
            raise AgentError("Decision must start with an explicit Rating field")
    return {key: value.strip() for key, value in fields.items()}


class TradingAgentsStrategy:
    version = "tradingagents-adapter-v2@" + UPSTREAM_COMMIT

    def __init__(self, config: dict):
        self.config = dict(config)
        self.model = ":".join(
            str(config.get(k) or "unconfigured")
            for k in ("llm_provider", "deep_think_llm", "quick_think_llm")
        )
        if config.get("asset_type") != "crypto":
            raise AgentError("TradingAgents adapter requires explicit crypto mode")
        if config.get("upstream_commit") != UPSTREAM_COMMIT:
            raise AgentError("TradingAgents adapter requires its pinned upstream commit")
        if config.get("decision_profile") not in DECISION_PROFILES:
            raise AgentError("TradingAgents adapter requires an explicit decision profile")
        for name in ("timeout_seconds", "decision_ttl_seconds"):
            if type(config.get(name)) is not int or not 1 <= config[name] <= 3600:
                raise AgentError(f"Invalid TradingAgents {name}")
        targets = config.get("rating_target_pct")
        if not isinstance(targets, dict) or set(targets) != RATINGS - {"Hold"}:
            raise AgentError("All four actionable rating targets must be configured")
        self.targets = {key: decimal(value, "rating target") for key, value in targets.items()}
        if not all(0 <= target <= 1 for target in self.targets.values()):
            raise AgentError("Rating targets must be equity fractions in [0, 1]")
        if not (
            self.targets["Sell"]
            == 0
            <= self.targets["Underweight"]
            <= self.targets["Overweight"]
            <= self.targets["Buy"]
        ):
            raise AgentError("Rating targets must be ordered with Sell equal to zero")

    def _decision(
        self,
        started_at: datetime,
        reason: str,
        *,
        rating: str = "REVIEW",
        target: Decimal | None = None,
        evidence: tuple[str, ...] = (),
        actionable: bool = False,
        symbol: str = "BTC/USD",
    ) -> TradeDecision:
        return TradeDecision(
            symbol=normalize_symbol(symbol),
            target_position_pct=target,
            reason=reason,
            expires_at=started_at + timedelta(seconds=self.config["decision_ttl_seconds"]),
            created_at=started_at,
            rating=rating,
            strategy_version=self.version,
            model=self.model,
            evidence=evidence,
            actionable=actionable,
        )

    def parse_output(
        self,
        output: object,
        market: MarketSnapshot,
        portfolio: PortfolioSnapshot,
        started_at: datetime,
        *,
        now: datetime | None = None,
    ) -> TradeDecision:
        """Buy/Overweight set a floor; Underweight a ceiling; Sell zero; Hold no order."""
        now = timestamp(now or utcnow())
        started_at = timestamp(started_at)
        if now >= started_at + timedelta(seconds=self.config["decision_ttl_seconds"]):
            return self._decision(
                started_at, "Analysis exceeded its decision validity window", symbol=market.symbol
            )
        if (
            not isinstance(output, dict)
            or not isinstance(output.get("signal"), str)
            or output["signal"] not in RATINGS
        ):
            return self._decision(
                started_at, "Upstream returned REVIEW or an invalid signal", symbol=market.symbol
            )
        report = output.get("final_trade_decision")
        market_report = output.get("market_report")
        if not isinstance(report, str) or not isinstance(market_report, str):
            return self._decision(
                started_at, "Required decision or market evidence is absent", symbol=market.symbol
            )
        try:
            fields = _sections(report)
        except AgentError:
            return self._decision(
                started_at, "Decision schema could not be parsed unambiguously", symbol=market.symbol
            )
        rating = fields.get("Rating", "").strip("*")
        if rating not in RATINGS or rating != output["signal"]:
            return self._decision(
                started_at,
                "Explicit rating is missing or disagrees with upstream signal",
                symbol=market.symbol,
            )
        if (
            any(fields.get(key, "").lower() in _MISSING for key in ("Executive Summary", "Investment Thesis"))
            or market_report.strip().lower() in _MISSING
        ):
            return self._decision(
                started_at,
                "Required executive summary, thesis or market evidence is absent",
                symbol=market.symbol,
            )
        if portfolio.equity_usd <= 0 or market.price <= 0:
            return self._decision(
                started_at, "Positive account equity and quote are required", symbol=market.symbol
            )
        current = portfolio.quantity_for(market.symbol) * market.price / portfolio.equity_usd
        if not 0 <= current <= 1:
            return self._decision(
                started_at,
                "Existing allocation is outside long-only unleveraged bounds",
                symbol=market.symbol,
            )
        target = current if rating == "Hold" else self.targets[rating]
        if rating in {"Buy", "Overweight"}:
            target = max(target, current)
        elif rating == "Underweight":
            target = min(target, current)
        return self._decision(
            started_at,
            fields["Executive Summary"][:4000],
            rating=rating,
            target=target,
            actionable=rating != "Hold",
            symbol=market.symbol,
            evidence=(
                fields["Investment Thesis"][:8000],
                market_report[:8000],
                f"Broker quote {market.price} USD at {market.observed_at.isoformat()}; account at {portfolio.observed_at.isoformat()}",
                f"Rating mapping: {rating}; actual allocation {current}; target allocation {target}",
            ),
        )

    def decide(self, market: MarketSnapshot, portfolio: PortfolioSnapshot) -> TradeDecision:
        started_at = utcnow()
        normalize_symbol(market.symbol)
        if portfolio.open_orders:
            return self._decision(
                started_at, "Open orders must be reconciled before AI analysis", symbol=market.symbol
            )
        verify_upstream(self.config["upstream_repository"], self.config["upstream_commit"])
        if any(not self.config.get(key) for key in ("llm_provider", "deep_think_llm", "quick_think_llm")):
            raise AgentError("Model provider and both model identifiers are required")
        payload = {
            "repository": self.config["upstream_repository"],
            "config": {key: self.config.get(key) for key in _GRAPH_CONFIG},
            "selected_analysts": self.config["selected_analysts"],
            "trade_date": started_at.date().isoformat(),
            "ticker": upstream_symbol(market.symbol),
            "portfolio": {
                "cash": str(min(portfolio.cash_usd, portfolio.buying_power_usd)),
                "currency": "USD",
                "positions": [
                    {
                        "ticker": upstream_symbol(p.symbol),
                        "quantity": str(p.quantity),
                        "average_price": str(p.average_entry_price),
                    }
                    for p in portfolio.positions
                ],
            },
            "snapshot_context": (
                f"Alpaca Paper broker snapshot (UTC): account {portfolio.observed_at.isoformat()}, "
                f"equity {portfolio.equity_usd} USD, cash {portfolio.cash_usd} USD, "
                f"buying power {portfolio.buying_power_usd} USD; "
                f"{market.symbol} quote {market.price} USD, bid {market.bid}, ask {market.ask}, "
                f"observed {market.observed_at.isoformat()}, source {market.source}. "
                "No open orders. Only the configured long crypto/USD spot pairs without leverage are allowed. "
                + DECISION_PROFILES[self.config["decision_profile"]]
            ),
        }
        env = {key: value for key, value in os.environ.items() if key in _MODEL_ENV | _RUNTIME_ENV}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1")
        worker = Path(__file__).with_name("_tradingagents_worker.py")
        try:
            with TemporaryDirectory(prefix="crypto-agent-ai-") as directory:
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
                    started_at,
                    "AI worker failed; verify optional dependencies and provider configuration",
                    symbol=market.symbol,
                )
            output = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            return self._decision(
                started_at, "AI analysis timed out; no order may be produced", symbol=market.symbol
            )
        except (OSError, ValueError):
            return self._decision(
                started_at, "AI worker response unavailable or malformed", symbol=market.symbol
            )
        return self.parse_output(output, market, portfolio, started_at)
