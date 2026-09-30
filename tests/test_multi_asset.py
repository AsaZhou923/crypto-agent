"""Three-symbol portfolio and rotation checks using deterministic local fixtures."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import crypto_agent.automation as automation_module
import crypto_agent.runner as runner_module
from crypto_agent.automation import Automation
from crypto_agent.config import load_settings
from crypto_agent.models import (
    Activity,
    AssetRules,
    MarketSnapshot,
    PortfolioSnapshot,
    Position,
    TradeDecision,
    utcnow,
)
from crypto_agent.risk.checks import validate_inputs, validate_order
from crypto_agent.storage.database import Database

D = Decimal
SYMBOLS = ("BTC/USD", "ETH/USD", "SOL/USD")
PRICES = {"BTC/USD": D("50000"), "ETH/USD": D("2500"), "SOL/USD": D("100")}


class FixtureBroker:
    mode = "offline"
    symbols = SYMBOLS

    def __init__(self):
        self.portfolio = PortfolioSnapshot(D(9700), D(10000), (), utcnow(), D(9700))

    def get_markets(self, symbols):
        now = utcnow()
        return {
            symbol: MarketSnapshot(
                symbol,
                PRICES[symbol],
                now,
                PRICES[symbol] * D(".999"),
                PRICES[symbol] * D("1.001"),
                "fixture",
            )
            for symbol in symbols
        }

    def get_market(self, symbol="BTC/USD"):
        return self.get_markets((symbol,))[symbol]

    def get_asset_rules(self, symbol="BTC/USD"):
        return AssetRules(symbol, D(".000000001"), D(".000000001"), D(".000000001"))

    def get_portfolio(self):
        return replace(self.portfolio, observed_at=utcnow())

    def get_activities(self, after=None):
        return []

    def get_order(self, client_order_id):
        return None

    def close(self):
        pass


def multi_settings(tmp_path):
    settings = load_settings(Path("config/demo"), mode="offline")
    paper = {
        **settings.paper,
        "symbols": list(SYMBOLS),
        "trigger": "scheduled",
        "trading_enabled": True,
        "database_path": str(tmp_path / "multi.sqlite"),
    }
    risk = {
        **settings.risk,
        "allowed_symbols": list(SYMBOLS),
        "price_bounds_usd": {
            "BTC/USD": {"min": D(100), "max": D(1000000)},
            "ETH/USD": {"min": D(1), "max": D(100000)},
            "SOL/USD": {"min": D(1), "max": D(10000)},
        },
        "max_position_pct": D(".20"),
        "max_total_position_pct": D(".30"),
        "min_price_usd": D("1"),
    }
    return replace(settings, paper=paper, risk=risk)


def sell_strategy(settings):
    def decide(market, portfolio):
        now = utcnow()
        return TradeDecision(
            market.symbol,
            D(0),
            "fixture sell",
            now + timedelta(seconds=120),
            now,
            "Sell",
            "fixture-v1",
            "fixture-model",
            ("fixture evidence",),
            True,
        )

    return Mock(decide=Mock(side_effect=decide))


def test_selected_market_waits_for_real_fresh_orderbook(monkeypatch):
    now = utcnow()
    stale = {
        symbol: MarketSnapshot(
            symbol,
            PRICES[symbol],
            now - timedelta(minutes=2) if symbol == "SOL/USD" else now,
            PRICES[symbol] * D(".999"),
            PRICES[symbol] * D("1.001"),
            "fixture-orderbook",
        )
        for symbol in SYMBOLS
    }
    fresh = {**stale, "SOL/USD": replace(stale["SOL/USD"], observed_at=now)}
    broker = Mock()
    broker.get_markets.side_effect = [stale, fresh]
    monkeypatch.setattr(runner_module, "utcnow", lambda: now)
    monkeypatch.setattr(runner_module, "monotonic", Mock(side_effect=[0, 0]))
    wait = Mock()
    monkeypatch.setattr(runner_module, "sleep", wait)

    result = runner_module._markets(broker, list(SYMBOLS), "SOL/USD", D(60), 30)

    assert result["SOL/USD"].observed_at == now
    assert broker.get_markets.call_count == 2
    wait.assert_called_once_with(5)


def test_aggregate_limit_blocks_new_exposure_but_allows_reduction(tmp_path):
    settings = multi_settings(tmp_path)
    market = FixtureBroker().get_market("SOL/USD")
    asset = AssetRules("SOL/USD", D(".001"), D(".001"), D(".01"))
    now = utcnow()
    portfolio = PortfolioSnapshot(
        D(7000),
        D(10000),
        (Position("BTC/USD", D(".04"), D(50000), D(".04")), Position("ETH/USD", D(".5"), D(2500), D(".5"))),
        now,
        D(7000),
    )
    buy = TradeDecision(
        "SOL/USD", D(".10"), "buy", now + timedelta(seconds=120), now, "Buy", "v", "m", ("e",)
    )
    result = validate_inputs(
        buy,
        market,
        portfolio,
        asset,
        settings.risk,
        D(10000),
        now,
        market_prices=PRICES,
    )
    assert not result.allowed and "Aggregate" in result.reasons[0]
    sell = replace(buy, symbol="BTC/USD", target_position_pct=D(".1"), rating="Underweight")
    btc_market = FixtureBroker().get_market("BTC/USD")
    assert validate_inputs(
        sell,
        btc_market,
        portfolio,
        replace(asset, symbol="BTC/USD"),
        settings.risk,
        D(10000),
        now,
        market_prices=PRICES,
    ).allowed


def test_doge_price_uses_its_own_sanity_range(tmp_path):
    settings = multi_settings(tmp_path)
    risk = {
        **settings.risk,
        "allowed_symbols": ["BTC/USD", "SOL/USD", "DOGE/USD"],
        "min_price_usd": D(".001"),
        "price_bounds_usd": {
            "BTC/USD": {"min": D(100), "max": D(1000000)},
            "SOL/USD": {"min": D(1), "max": D(10000)},
            "DOGE/USD": {"min": D(".001"), "max": D(10)},
        },
    }
    now = utcnow()
    market = MarketSnapshot("DOGE/USD", D(".10"), now, D(".0999"), D(".1001"), "fixture")
    asset = AssetRules("DOGE/USD", D(".000001"), D(".000001"), D(".000001"))
    portfolio = PortfolioSnapshot(D(10000), D(10000), (), now, D(10000))
    decision = TradeDecision(
        "DOGE/USD",
        D(".001"),
        "buy",
        now + timedelta(seconds=120),
        now,
        "Buy",
        "v",
        "m",
        ("e",),
    )
    prices = {"BTC/USD": D(50000), "SOL/USD": D(100), "DOGE/USD": D(".10")}
    assert validate_inputs(
        decision, market, portfolio, asset, risk, D(10000), now, market_prices=prices
    ).allowed


def test_order_cannot_push_combined_portfolio_over_total_limit(tmp_path):
    settings = multi_settings(tmp_path)
    settings = replace(settings, risk={**settings.risk, "max_total_position_pct": D(".25")})
    broker = FixtureBroker()
    now = utcnow()
    broker.portfolio = PortfolioSnapshot(
        D(8000), D(10000), (Position("BTC/USD", D(".04"), D(50000), D(".04")),), now, D(8000)
    )
    market = broker.get_market("ETH/USD")
    decision = TradeDecision(
        "ETH/USD", D(".05"), "buy", now + timedelta(seconds=120), now, "Buy", "v", "m", ("e",)
    )
    from crypto_agent.execution.planner import plan_orders

    order = plan_orders(
        decision,
        market,
        broker.portfolio,
        broker.get_asset_rules("ETH/USD"),
        settings.risk,
        "multi-order",
        market_prices=PRICES,
    )[0]
    result = validate_order(
        order,
        decision,
        market,
        broker.portfolio,
        broker.get_asset_rules("ETH/USD"),
        settings.risk,
        D(10000),
        now,
        market_prices=PRICES,
    )
    assert not result.allowed and "aggregate" in result.reasons[0]


def test_automatic_cycles_rotate_three_symbols_durably(tmp_path, monkeypatch):
    settings = multi_settings(tmp_path)
    db = Database(settings.database_path, "offline")
    broker = FixtureBroker()
    automation = Automation(settings, db)
    strategy = sell_strategy(settings)
    now = utcnow()
    monkeypatch.setattr(automation_module, "utcnow", lambda: now)
    try:
        automation.enable(explicit=True)
        results = []
        for _ in range(3):
            results.append(automation.tick(broker, explicit=True, strategy=strategy))
            now += timedelta(seconds=600)
        assert [result["symbol"] for result in results] == list(SYMBOLS)
        assert all(result["status"] == "no_order" for result in results)
        assert [call.args[0].symbol for call in strategy.decide.call_args_list] == list(SYMBOLS)
        restarted = Automation(settings, db)
        assert restarted._state()["next_symbol_index"] == 0
    finally:
        db.close()


def test_multi_symbol_report_marks_each_position_with_its_own_market(tmp_path):
    db = Database(tmp_path / "report.sqlite", "paper")
    now = utcnow()
    opening = PortfolioSnapshot(D(10000), D(10000), (), now, D(10000), account_id="paper")
    opening_markets = {
        symbol: MarketSnapshot(symbol, price, now, price, price, "fixture")
        for symbol, price in PRICES.items()
    }
    db.snapshot(opening, opening_markets)
    db.activities(
        [
            Activity(
                "eth-fill",
                "FILL",
                now + timedelta(seconds=1),
                "ETH/USD",
                "eth-order",
                "buy",
                D(".1"),
                D(2500),
            ),
            Activity(
                "sol-fill", "FILL", now + timedelta(seconds=2), "SOL/USD", "sol-order", "buy", D(1), D(100)
            ),
            Activity(
                "eth-sell",
                "FILL",
                now + timedelta(seconds=3),
                "ETH/USD",
                "eth-sell-order",
                "sell",
                D(".05"),
                D(2700),
            ),
            Activity(
                "eth-fee",
                "CFEE",
                now + timedelta(seconds=4),
                "ETH/USD",
                "eth-fee-order",
                quantity=D("-.001"),
                price=D(2500),
                fee_usd=D("2.5"),
            ),
        ]
    )
    later = now + timedelta(seconds=5)
    final = PortfolioSnapshot(
        D(9785),
        D(10025),
        (Position("ETH/USD", D(".049"), D(2500), D(".049")), Position("SOL/USD", D(1), D(100), D(1))),
        later,
        D(9650),
        account_id="paper",
    )
    final_markets = {
        "BTC/USD": MarketSnapshot("BTC/USD", D(50000), later, D(50000), D(50000), "fixture"),
        "ETH/USD": MarketSnapshot("ETH/USD", D(2600), later, D(2600), D(2600), "fixture"),
        "SOL/USD": MarketSnapshot("SOL/USD", D(110), later, D(110), D(110), "fixture"),
    }
    db.snapshot(final, final_markets, establish_baseline=False)
    try:
        report = db.report()
        assert report["realized_pnl_gross_usd"] == D(10)
        assert report["unrealized_pnl_usd"] == D("14.900")
        assert report["recorded_fees_usd"] == D("2.5")
        assert report["fill_count"] == 3
        assert report["ledger_matches_position"]
        assert report["position_quantity_differences"] == {}
        assert set(report["market_observed_at_by_symbol"]) == set(SYMBOLS)
    finally:
        db.close()


def test_delayed_fee_activity_refreshes_authoritative_asset_fields(tmp_path):
    db = Database(tmp_path / "fees.sqlite", "paper")
    now = utcnow()
    opening = PortfolioSnapshot(
        D(9995),
        D(10000),
        (Position("BTC/USD", D(".0001"), D(50000), D(".0001")),),
        now,
        D(9995),
        account_id="paper",
    )
    market = MarketSnapshot("BTC/USD", D(50000), now, D(50000), D(50000), "fixture")
    db.snapshot(opening, {"BTC/USD": market})
    db.activities(
        [
            Activity(
                "delayed-fee",
                "CFEE",
                now + timedelta(seconds=1),
                fee_usd=D("5"),
                currency="USD",
            )
        ]
    )
    final = PortfolioSnapshot(
        D("9995"), D("9995"), (), now + timedelta(seconds=2), D("9995"), account_id="paper"
    )
    db.snapshot(
        final, {"BTC/USD": replace(market, observed_at=now + timedelta(seconds=2))}, establish_baseline=False
    )
    try:
        report = db.report()
        assert report["recorded_fees_usd"] == D("5")
        assert not report["ledger_matches_position"]
        assert report["position_quantity_differences"] == {"BTC/USD": "0.0001"}
        assert report["realized_pnl_gross_usd"] is None
        assert "not treated as proven pending fees" in report["pending_fee_notice"]
        db.activities(
            [
                Activity(
                    "delayed-fee",
                    "CFEE",
                    now + timedelta(seconds=1),
                    "BTC/USD",
                    quantity=D("-0.0001"),
                    price=D(50000),
                )
            ]
        )
        report = db.report()
        assert report["recorded_fees_usd"] == D("5")
        assert report["unvalued_fee_records"] == 0
        assert report["ledger_matches_position"]
        assert report["position_quantity_differences"] == {}
    finally:
        db.close()


def test_delayed_fee_activity_refreshes_cash_fields_without_losing_asset_fields(tmp_path):
    db = Database(tmp_path / "fees-reverse.sqlite", "paper")
    now = utcnow()
    opening = PortfolioSnapshot(
        D(9995),
        D(10000),
        (Position("BTC/USD", D(".0001"), D(50000), D(".0001")),),
        now,
        D(9995),
        account_id="paper",
    )
    market = MarketSnapshot("BTC/USD", D(50000), now, D(50000), D(50000), "fixture")
    db.snapshot(opening, {"BTC/USD": market})
    db.activities(
        [
            Activity(
                "delayed-fee",
                "CFEE",
                now + timedelta(seconds=1),
                "BTC/USD",
                quantity=D("-0.0001"),
                price=D(50000),
            )
        ]
    )
    final = PortfolioSnapshot(
        D("9995"), D("9995"), (), now + timedelta(seconds=2), D("9995"), account_id="paper"
    )
    db.snapshot(
        final, {"BTC/USD": replace(market, observed_at=now + timedelta(seconds=2))}, establish_baseline=False
    )
    try:
        report = db.report()
        assert report["ledger_matches_position"]
        assert report["unvalued_fee_records"] == 1
        db.activities(
            [
                Activity(
                    "delayed-fee",
                    "CFEE",
                    now + timedelta(seconds=1),
                    fee_usd=D("5"),
                    currency="USD",
                )
            ]
        )
        report = db.report()
        assert report["recorded_fees_usd"] == D("5")
        assert report["unvalued_fee_records"] == 0
        assert report["ledger_matches_position"]
        assert report["position_quantity_differences"] == {}
    finally:
        db.close()


def test_positive_cfee_quantity_fails_closed_without_inventing_debit(tmp_path):
    db = Database(tmp_path / "positive-fee.sqlite", "paper")
    now = utcnow()
    opening = PortfolioSnapshot(
        D(10000),
        D(10000),
        (Position("BTC/USD", D(".0001"), D(50000), D(".0001")),),
        now,
        D(10000),
        account_id="paper",
    )
    market = MarketSnapshot("BTC/USD", D(50000), now, D(50000), D(50000), "fixture")
    db.snapshot(opening, {"BTC/USD": market})
    db.activities(
        [
            Activity(
                "fee-credit",
                "CFEE",
                now + timedelta(seconds=1),
                "BTC/USD",
                quantity=D("0.0001"),
                price=D(50000),
                fee_usd=D("5"),
                currency="USD",
            )
        ]
    )
    final = PortfolioSnapshot(
        D(10000),
        D(10000),
        (Position("BTC/USD", D(".0001"), D(50000), D(".0001")),),
        now + timedelta(seconds=2),
        D(10000),
        account_id="paper",
    )
    db.snapshot(
        final, {"BTC/USD": replace(market, observed_at=now + timedelta(seconds=2))}, establish_baseline=False
    )
    try:
        report = db.report()
        assert not report["ledger_matches_position"]
        assert report["position_quantity_differences"] == {}
        assert report["realized_pnl_gross_usd"] is None
    finally:
        db.close()
