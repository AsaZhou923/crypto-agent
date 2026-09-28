"""Private process boundary; never prints provider errors, credentials or traces."""

import json
import logging
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path


def run(payload: dict) -> dict:
    sys.path.insert(0, str(Path(payload["repository"]).resolve()))
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.portfolio import PortfolioContext

    class BrokerPortfolioContext(PortfolioContext):
        snapshot_context: str

        def render(self, ticker: str) -> str:
            return super().render(ticker) + "\n\n" + self.snapshot_context

    config = deepcopy(DEFAULT_CONFIG)
    config.update(payload["config"])
    root = Path.cwd()
    config.update(
        results_dir=str(root / "results"),
        data_cache_dir=str(root / "cache"),
        memory_log_path=str(root / "memory.md"),
        checkpoint_enabled=False,
        llm_max_retries=0,
    )
    portfolio = BrokerPortfolioContext.model_validate(
        {**payload["portfolio"], "snapshot_context": payload["snapshot_context"]}
    )
    graph = TradingAgentsGraph(selected_analysts=payload["selected_analysts"], debug=False, config=config)
    state, signal = graph.propagate(
        payload.get("ticker", "BTC-USD"),
        payload["trade_date"],
        asset_type="crypto",
        portfolio=portfolio,
    )
    return {
        "signal": signal,
        "final_trade_decision": state.get("final_trade_decision"),
        "market_report": state.get("market_report"),
    }


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        payload = json.load(sys.stdin)
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            output = run(payload)
        serialized = json.dumps(output, ensure_ascii=False)
        for key, value in os.environ.items():
            if any(part in key for part in ("API_KEY", "ACCESS_KEY", "SECRET", "TOKEN")) and len(value) >= 4:
                # Scrub the JSON-escaped representation too: credentials may
                # contain quotes or backslashes, which serialization escapes.
                escaped = json.dumps(value, ensure_ascii=False)[1:-1]
                serialized = serialized.replace(escaped, "[REDACTED]")
        sys.stdout.write(serialized)
        return 0
    except Exception:
        sys.stdout.write('{"error":"AI worker failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
