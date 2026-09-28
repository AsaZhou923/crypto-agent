"""Optional installed upstream schema/import smoke; no LLM or platform requests."""

from pathlib import Path

import pytest

from crypto_agent.brokers.offline import OfflineBroker
from crypto_agent.config import load_settings
from crypto_agent.models import utcnow
from crypto_agent.strategies.tradingagents import TradingAgentsStrategy


def test_real_installed_upstream_schema_parses(tmp_path):
    schemas = pytest.importorskip("tradingagents.agents.schemas")
    pytest.importorskip("tradingagents.graph.trading_graph")
    strategy = TradingAgentsStrategy(load_settings(Path("config/demo"), mode="offline").strategy)
    pm = schemas.PortfolioDecision(
        rating="Buy", executive_summary="Synthetic schema test", investment_thesis="Synthetic evidence only"
    )
    broker = OfflineBroker(tmp_path / "broker.sqlite")
    decision = strategy.parse_output(
        {
            "signal": "Buy",
            "final_trade_decision": schemas.render_pm_decision(pm),
            "market_report": "Test fixture market evidence",
        },
        broker.get_market(),
        broker.get_portfolio(),
        utcnow(),
    )
    assert decision.actionable
    assert decision.rating == "Buy"
    broker.close()
