"""Strict local configuration. Credentials stay in process environment only."""

import os
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv

from crypto_agent.models import (
    SUPPORTED_SYMBOLS,
    SYMBOL,
    UPSTREAM_COMMIT,
    AgentError,
    decimal,
    dumps,
    normalize_symbol,
    upstream_symbol,
)

PAPER_URL = "https://paper-api.alpaca.markets"
MODEL_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
RISK_NUMBERS = (
    "max_position_pct",
    "max_total_position_pct",
    "max_order_notional_usd",
    "min_order_notional_usd",
    "max_daily_loss_usd",
    "max_data_age_seconds",
    "max_decision_age_seconds",
    "fee_buffer_bps",
    "slippage_bps",
    "max_spread_bps",
    "min_price_usd",
    "max_price_usd",
)


@dataclass(frozen=True)
class Settings:
    paper: dict
    strategy: dict
    risk: dict
    root: Path
    mode: str

    @property
    def database_path(self) -> Path:
        return self.root / self.paper["database_path"]

    @property
    def summary(self) -> dict:
        # Only validated allowlisted fields; never environment or credential values.
        result = {"paper": self.paper, "strategy": self.strategy, "risk": self.risk, "mode": self.mode}
        if self.strategy.get("name") == "intraday_ai":
            # Bind the corrected minute-clock semantics to previews and approval.
            result["intraday_feature_contract"] = "contiguous-60-second-bars-v1"
        return result

    @property
    def digest(self) -> str:
        return sha256(dumps(self.summary).encode()).hexdigest()


def _mapping(path: Path, allowed: set[str]) -> dict:
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise AgentError(f"Cannot load configuration file: {path.name}") from exc
    if not isinstance(value, dict) or set(value) - allowed:
        raise AgentError(f"Unknown fields or invalid mapping in {path.name}")
    return value


def _required(values: dict, names, label: str) -> None:
    missing = [name for name in names if values.get(name) is None]
    if missing:
        raise AgentError(f"Missing required {label}: {', '.join(missing)}")


def _backend_url(value: object) -> str | None:
    """Keep endpoints in the config digest without permitting embedded credentials."""
    if value is None:
        return None
    error = "backend_url must be an HTTPS URL without credentials, query, fragment or whitespace"
    if not isinstance(value, str) or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise AgentError(error)
    if any(character in value for character in ("\\", "?", "#")):
        raise AgentError(error)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid endpoint")
        # Access validates malformed or out-of-range port values too.
        _ = parsed.port
    except ValueError:
        raise AgentError(error) from None
    return value.rstrip("/")


def validate_risk(risk: dict) -> dict:
    if type(risk.get("cap_order_to_limit", False)) is not bool:
        raise AgentError("cap_order_to_limit must be a YAML boolean")
    _required(
        risk,
        (
            *RISK_NUMBERS,
            "allowed_symbols",
            "price_bounds_usd",
            "allow_leverage",
            "allow_short",
            "require_order_preview",
        ),
        "risk limits",
    )
    if risk["allow_leverage"] is not False or risk["allow_short"] is not False:
        raise AgentError("Leverage and short selling must be false")
    try:
        symbols = [normalize_symbol(value) for value in risk["allowed_symbols"]]
    except (AgentError, TypeError):
        raise AgentError("Risk whitelist requires one to three supported USD pairs") from None
    if (
        risk["require_order_preview"] is not True
        or not 1 <= len(symbols) <= 3
        or len(set(symbols)) != len(symbols)
    ):
        raise AgentError("Risk requires previews and one to three distinct supported USD pairs")
    result = dict(risk)
    for key in ("max_position_notional_usd", "max_total_position_notional_usd"):
        if key in result:
            result[key] = decimal(result[key], key)
            if result[key] <= 0:
                raise AgentError(f"Risk limit must be positive: {key}")
    if (
        "max_position_notional_usd" in result
        and "max_total_position_notional_usd" in result
        and result["max_position_notional_usd"] > result["max_total_position_notional_usd"]
    ):
        raise AgentError("Per-symbol dollar cap exceeds aggregate dollar cap")
    result["allowed_symbols"] = symbols
    bounds = risk["price_bounds_usd"]
    if not isinstance(bounds, dict) or set(bounds) != set(symbols):
        raise AgentError("price_bounds_usd requires exact bounds for each allowed symbol")
    result["price_bounds_usd"] = {}
    for symbol, value in bounds.items():
        if not isinstance(value, dict) or set(value) != {"min", "max"}:
            raise AgentError("Each symbol price bound requires exactly min and max")
        minimum, maximum = (
            decimal(value["min"], f"{symbol} min price"),
            decimal(value["max"], f"{symbol} max price"),
        )
        if minimum <= 0 or minimum >= maximum:
            raise AgentError("Invalid per-symbol price sanity range")
        result["price_bounds_usd"][symbol] = {"min": minimum, "max": maximum}
    for name in RISK_NUMBERS:
        result[name] = decimal(risk[name], name)
        if result[name] <= 0:
            raise AgentError(f"Risk limit must be positive: {name}")
    if result["max_position_pct"] > 1:
        raise AgentError("max_position_pct must be in (0, 1]")
    if not result["max_position_pct"] <= result["max_total_position_pct"] <= 1:
        raise AgentError("max_total_position_pct must be between the per-symbol limit and 1")
    if result["min_order_notional_usd"] > result["max_order_notional_usd"]:
        raise AgentError("Minimum order exceeds maximum order")
    if result["min_price_usd"] >= result["max_price_usd"]:
        raise AgentError("Invalid price sanity range")
    if any(result[key] >= 1000 for key in ("fee_buffer_bps", "slippage_bps", "max_spread_bps")):
        raise AgentError("Fee, slippage and spread buffers must be below 1000 bps")
    return result


def validate_intraday_trade_filter(strategy: dict) -> str:
    trade_filter = strategy.get("intraday_trade_filter", "none")
    if not isinstance(trade_filter, str) or trade_filter not in {"none", "confirm2_cooldown30"}:
        raise AgentError("intraday_trade_filter must be none or confirm2_cooldown30")
    if trade_filter != "none" and (
        strategy.get("name") != "intraday_ai"
        or strategy.get("intraday_probe_profile") != "expanded_paper"
        or strategy.get("intraday_entry_policy") != "capped_probe"
    ):
        raise AgentError("intraday_trade_filter requires intraday_ai expanded_paper capped_probe")
    return trade_filter


def validate_intraday_entry_economics(strategy: dict) -> bool:
    enabled = strategy.get("intraday_require_cost_cover", False)
    if type(enabled) is not bool:
        raise AgentError("intraday_require_cost_cover must be a YAML boolean")
    cooldown = strategy.get("intraday_entry_cooldown_seconds")
    if cooldown is not None and (type(cooldown) is not int or not 60 <= cooldown <= 3600):
        raise AgentError("intraday_entry_cooldown_seconds must be an integer in [60, 3600]")
    if enabled and cooldown is None:
        raise AgentError("intraday_require_cost_cover requires intraday_entry_cooldown_seconds")
    if cooldown is not None and not enabled:
        raise AgentError("intraday_entry_cooldown_seconds requires intraday_require_cost_cover")
    if enabled and (
        strategy.get("name") != "intraday_ai"
        or strategy.get("intraday_probe_profile") != "expanded_paper"
        or strategy.get("intraday_entry_policy") != "capped_probe"
        or strategy.get("intraday_trade_filter") != "confirm2_cooldown30"
    ):
        raise AgentError(
            "intraday_require_cost_cover requires intraday_ai expanded_paper capped_probe confirm2_cooldown30"
        )
    return enabled


def validate_intraday_policy(strategy: dict) -> str:
    validate_intraday_trade_filter(strategy)
    validate_intraday_entry_economics(strategy)
    policy = strategy.get("intraday_entry_policy")
    if not isinstance(policy, str) or policy not in {"cost_cover", "capped_probe"}:
        raise AgentError("intraday_entry_policy must be cost_cover or capped_probe")
    if policy == "capped_probe":
        profile = strategy.get("intraday_probe_profile", "small")
        if profile not in ("small", "expanded_paper"):
            raise AgentError("intraday_probe_profile must be small or expanded_paper")
        expanded = profile == "expanded_paper"
        for key, maximum in (
            ("intraday_probe_max_position_usd", Decimal(5000) if expanded else Decimal(10)),
            ("intraday_probe_cost_budget_usd", Decimal(75) if expanded else Decimal("0.15")),
        ):
            value = decimal(strategy.get(key), key)
            if not 0 < value <= maximum:
                raise AgentError(f"{key} must be positive and at most {maximum}")
    return policy


def load_settings(
    config_dir: Path = Path("config"), mode: str = "paper", root: Path | None = None
) -> Settings:
    root = (root or Path.cwd()).resolve()
    if mode not in {"paper", "offline"}:
        raise AgentError("Mode must be paper or offline")
    config_dir = (root / config_dir).resolve()
    load_dotenv(root / ".env", override=False)
    paper = _mapping(
        config_dir / "paper.yaml",
        {
            "environment",
            "broker",
            "base_url",
            "trading_enabled",
            "symbols",
            "trigger",
            "database_path",
            "request_timeout_seconds",
            "market_data_source",
            "fresh_market_wait_seconds",
            "automatic_interval_seconds",
            "automatic_symbols_per_cycle",
        },
    )
    _required(
        paper,
        (
            "environment",
            "broker",
            "base_url",
            "trading_enabled",
            "symbols",
            "trigger",
            "database_path",
            "request_timeout_seconds",
            "market_data_source",
            "fresh_market_wait_seconds",
        ),
        "paper configuration",
    )
    if paper["base_url"] != PAPER_URL or paper["environment"] != "paper" or paper["broker"] != "alpaca_paper":
        raise AgentError("Only the exact Alpaca Paper endpoint is permitted")
    if os.environ.get("ALPACA_PAPER_TRADE", "true").lower() != "true":
        raise AgentError("ALPACA_PAPER_TRADE must be true")
    try:
        paper["symbols"] = [normalize_symbol(value) for value in paper["symbols"]]
    except (AgentError, TypeError):
        raise AgentError("Paper symbols require one to three supported USD pairs") from None
    if (
        not 1 <= len(paper["symbols"]) <= 3
        or len(set(paper["symbols"])) != len(paper["symbols"])
        or paper["trigger"] not in {"manual", "scheduled"}
    ):
        raise AgentError("Only one to three distinct supported spot symbols are permitted")
    if type(paper["trading_enabled"]) is not bool:
        raise AgentError("trading_enabled must be a YAML boolean")
    if paper["market_data_source"] != "alpaca_crypto_us_orderbook":
        raise AgentError("Paper market_data_source must be alpaca_crypto_us_orderbook")
    if not isinstance(paper["database_path"], str) or not paper["database_path"].strip():
        raise AgentError("database_path is required")
    _integer(paper, "request_timeout_seconds", 1, 120)
    _integer(paper, "fresh_market_wait_seconds", 0, 60)
    if "automatic_interval_seconds" in paper:
        _integer(paper, "automatic_interval_seconds", 60, 3600)
    if "automatic_symbols_per_cycle" in paper:
        _integer(paper, "automatic_symbols_per_cycle", 1, len(paper["symbols"]))
    strategy = _mapping(
        config_dir / "strategy.yaml",
        {
            "name",
            "upstream_repository",
            "upstream_commit",
            "analysis_symbol",
            "asset_type",
            "llm_provider",
            "backend_url",
            "deep_think_llm",
            "quick_think_llm",
            "selected_analysts",
            "max_debate_rounds",
            "max_risk_discuss_rounds",
            "output_language",
            "decision_profile",
            "timeout_seconds",
            "decision_ttl_seconds",
            "rating_target_pct",
            "baseline_buy_below_usd",
            "baseline_target_pct",
            "intraday_lookback_bars",
            "intraday_min_bars",
            "intraday_max_bar_age_seconds",
            "intraday_momentum_threshold_bps",
            "intraday_entry_score",
            "intraday_entry_policy",
            "intraday_probe_max_position_usd",
            "intraday_probe_cost_budget_usd",
            "intraday_probe_profile",
            "intraday_trade_filter",
            "intraday_require_cost_cover",
            "intraday_entry_cooldown_seconds",
            "intraday_bar_source",
            "intraday_max_quote_bar_deviation_bps",
        },
    )
    _required(
        strategy,
        (
            "name",
            "asset_type",
            "analysis_symbol",
            "upstream_commit",
            "upstream_repository",
            "timeout_seconds",
            "decision_ttl_seconds",
            "rating_target_pct",
            "baseline_buy_below_usd",
            "baseline_target_pct",
            "selected_analysts",
            "max_debate_rounds",
            "max_risk_discuss_rounds",
            "output_language",
            "decision_profile",
        ),
        "strategy configuration",
    )
    if strategy["name"] not in {"baseline", "tradingagents", "intraday_ai"}:
        raise AgentError("Strategy must be baseline, tradingagents or intraday_ai")
    bar_source = strategy.get("intraday_bar_source", "alpaca_crypto_us")
    if not isinstance(bar_source, str) or bar_source not in {"alpaca_crypto_us", "coinbase_exchange"}:
        raise AgentError("Unsupported intraday_bar_source")
    if bar_source == "coinbase_exchange":
        if strategy["name"] != "intraday_ai" or mode != "paper":
            raise AgentError("coinbase_exchange bars require Paper intraday_ai")
        _required(strategy, ("intraday_max_quote_bar_deviation_bps",), "cross-venue configuration")
        _integer(strategy, "intraday_max_quote_bar_deviation_bps", 1, 100)
        if strategy.get("intraday_require_cost_cover") is not True:
            raise AgentError("coinbase_exchange bars require intraday_require_cost_cover")
    elif "intraday_max_quote_bar_deviation_bps" in strategy:
        raise AgentError("intraday_max_quote_bar_deviation_bps requires coinbase_exchange bars")
    trade_filter = validate_intraday_trade_filter(strategy)
    if trade_filter != "none" and mode != "paper":
        raise AgentError("intraday_trade_filter is Paper-only")
    entry_economics = validate_intraday_entry_economics(strategy)
    if entry_economics and mode != "paper":
        raise AgentError("intraday_require_cost_cover is Paper-only")
    if strategy["asset_type"] != "crypto":
        raise AgentError("TradingAgents requires explicit crypto mode")
    if strategy["analysis_symbol"] not in {upstream_symbol(value) for value in SUPPORTED_SYMBOLS}:
        raise AgentError("analysis_symbol must be a supported crypto symbol")
    if strategy["upstream_commit"] != UPSTREAM_COMMIT:
        raise AgentError("Unsupported TradingAgents revision; update requires adapter validation")
    if not isinstance(strategy["upstream_repository"], str):
        raise AgentError("upstream_repository must be a local path")
    strategy["upstream_repository"] = str((root / strategy["upstream_repository"]).resolve())
    for key in ("llm_provider", "deep_think_llm", "quick_think_llm"):
        env = os.environ.get("TRADINGAGENTS_" + key.upper())
        if env:
            strategy[key] = env
        if strategy.get(key) is not None and (
            not isinstance(strategy[key], str) or not strategy[key].strip()
        ):
            raise AgentError(f"Invalid model setting: {key}")
    if strategy.get("llm_provider") is not None:
        strategy["llm_provider"] = strategy["llm_provider"].lower()
        if strategy["llm_provider"] not in MODEL_KEYS:
            raise AgentError("Supported model providers: openai, anthropic, google, deepseek")
    backend_url = os.environ.get("TRADINGAGENTS_LLM_BACKEND_URL")
    if not backend_url and strategy.get("llm_provider") == "openai":
        backend_url = os.environ.get("OPENAI_BASE_URL")
    strategy["backend_url"] = _backend_url(backend_url or strategy.get("backend_url"))
    if strategy["selected_analysts"] != ["market"]:
        raise AgentError("MVP supports only the crypto market analyst")
    for key in ("timeout_seconds", "decision_ttl_seconds"):
        _integer(strategy, key, 1, 3600)
    for key in ("max_debate_rounds", "max_risk_discuss_rounds"):
        _integer(strategy, key, 1, 3)
    if not isinstance(strategy["output_language"], str) or not strategy["output_language"].strip():
        raise AgentError("output_language must be nonempty")
    if strategy["name"] == "intraday_ai":
        _required(
            strategy,
            (
                "intraday_lookback_bars",
                "intraday_min_bars",
                "intraday_max_bar_age_seconds",
                "intraday_momentum_threshold_bps",
                "intraday_entry_score",
                "intraday_entry_policy",
            ),
            "intraday strategy configuration",
        )
        if strategy["decision_profile"] != "intraday_10m":
            raise AgentError("intraday_ai requires decision_profile: intraday_10m")
        if strategy.get("llm_provider") != "openai":
            raise AgentError("intraday_ai currently requires llm_provider: openai")
        _integer(strategy, "intraday_lookback_bars", 31, 239)
        _integer(strategy, "intraday_min_bars", 31, strategy["intraday_lookback_bars"])
        _integer(strategy, "intraday_max_bar_age_seconds", 1, 600)
        _integer(strategy, "intraday_entry_score", 1, 4)
        validate_intraday_policy(strategy)
        strategy["intraday_momentum_threshold_bps"] = decimal(
            strategy["intraday_momentum_threshold_bps"], "intraday_momentum_threshold_bps"
        )
        if not Decimal("0.1") <= strategy["intraday_momentum_threshold_bps"] <= Decimal(100):
            raise AgentError("intraday_momentum_threshold_bps must be in [0.1, 100]")
    elif strategy["decision_profile"] not in {"balanced", "short_term_small"}:
        raise AgentError("decision_profile must be balanced or short_term_small")
    strategy["baseline_buy_below_usd"] = decimal(strategy["baseline_buy_below_usd"], "baseline_buy_below_usd")
    strategy["baseline_target_pct"] = decimal(strategy["baseline_target_pct"], "baseline_target_pct")
    if strategy["baseline_buy_below_usd"] <= 0 or not 0 <= strategy["baseline_target_pct"] <= 1:
        raise AgentError("Invalid baseline parameters")
    targets = strategy["rating_target_pct"]
    if not isinstance(targets, dict) or set(targets) != {"Buy", "Overweight", "Underweight", "Sell"}:
        raise AgentError(
            "rating_target_pct requires exactly Buy, Overweight, Underweight, Sell; Hold preserves holdings"
        )
    strategy["rating_target_pct"] = {key: decimal(value, key) for key, value in targets.items()}
    if not all(0 <= value <= 1 for value in strategy["rating_target_pct"].values()):
        raise AgentError("Rating target positions must be in [0, 1]")
    t = strategy["rating_target_pct"]
    if not (t["Sell"] == 0 <= t["Underweight"] <= t["Overweight"] <= t["Buy"]):
        raise AgentError("Rating targets must be ordered and Sell must be zero")
    risk = validate_risk(
        _mapping(
            config_dir / "risk.yaml",
            {
                *RISK_NUMBERS,
                "allowed_symbols",
                "price_bounds_usd",
                "allow_leverage",
                "allow_short",
                "require_order_preview",
                "cap_order_to_limit",
                "max_position_notional_usd",
                "max_total_position_notional_usd",
            },
        )
    )
    if risk["allowed_symbols"] != paper["symbols"]:
        raise AgentError("Paper symbols and risk allowed_symbols must match exactly")
    if strategy.get("intraday_probe_profile") == "expanded_paper":
        if paper["symbols"] != ["BTC/USD", "XRP/USD"]:
            raise AgentError("Expanded Paper profile requires BTC/USD and XRP/USD only")
        for key, maximum in (
            ("max_position_pct", Decimal(".05")),
            ("max_total_position_pct", Decimal(".10")),
            ("max_order_notional_usd", Decimal(500)),
            ("max_daily_loss_usd", Decimal(500)),
            ("max_position_notional_usd", Decimal(5000)),
            ("max_total_position_notional_usd", Decimal(10000)),
        ):
            if key not in risk or risk[key] > maximum:
                raise AgentError(f"Expanded Paper profile exceeds authorized {key}")
    if (
        max(strategy["rating_target_pct"].values()) > risk["max_position_pct"]
        or strategy["baseline_target_pct"] > risk["max_position_pct"]
    ):
        raise AgentError("Strategy targets exceed position risk limit")
    if strategy["decision_ttl_seconds"] > risk["max_decision_age_seconds"]:
        raise AgentError("Decision TTL exceeds risk maximum decision age")
    if mode == "offline":
        if paper["symbols"] != [SYMBOL]:
            raise AgentError("Offline demo supports BTC/USD only")
        # Separate files, even when accidentally selecting the paper config directory.
        path = Path(paper["database_path"])
        paper["database_path"] = str(path.with_name(path.stem + "-offline" + path.suffix))
    return Settings(paper, strategy, risk, root, mode)


def _integer(values: dict, key: str, low: int, high: int) -> None:
    value = values.get(key)
    if type(value) is not int or not low <= value <= high:
        raise AgentError(f"{key} must be an integer in [{low}, {high}]")


def require_model(settings: Settings) -> None:
    _required(settings.strategy, ("llm_provider", "deep_think_llm", "quick_think_llm"), "model configuration")
    key = MODEL_KEYS[settings.strategy["llm_provider"]]
    if not os.environ.get(key):
        raise AgentError(f"Missing local model credential: {key}")
