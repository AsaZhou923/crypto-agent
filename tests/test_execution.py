"""Durable execution boundaries tested against the local broker, never the network."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from crypto_agent.brokers.offline import OfflineBroker
from crypto_agent.config import load_settings
from crypto_agent.execution.executor import execute_order
from crypto_agent.models import AgentError, OrderRejected, SubmissionUnknown, dumps, utcnow
from crypto_agent.runner import execute_preview, reconcile_account, run_once
from crypto_agent.storage.database import Database


@pytest.fixture
def setup(tmp_path):
    settings = load_settings(Path("config/demo"), mode="offline")
    settings = replace(settings, paper={**settings.paper, "database_path": str(tmp_path / "ledger.sqlite")})
    db = Database(settings.database_path, "offline")
    broker = OfflineBroker(tmp_path / "broker.sqlite")
    yield settings, db, broker
    broker.close()
    db.close()


def preview(setup):
    settings, db, broker = setup
    result = run_once(settings, broker, db)
    assert result["status"] == "preview", result
    return result


def test_preview_does_not_submit_and_explicit_flag_required(setup):
    settings, db, broker = setup
    result = preview(setup)
    assert not broker.get_portfolio().positions
    assert not broker.get_order(result["order"].client_order_id)
    with pytest.raises(AgentError, match="explicit"):
        execute_preview(settings, broker, db, result["run_id"])
    assert not broker.get_portfolio().positions


def test_duplicate_execute_and_reconcile_do_not_duplicate_fills(setup):
    settings, db, broker = setup
    result = preview(setup)
    spy = Mock(wraps=broker.submit_order)
    broker.submit_order = spy
    execute_preview(settings, broker, db, result["run_id"], True)
    original = broker.get_portfolio()
    execute_preview(settings, broker, db, result["run_id"], True)
    reconcile_account(broker, db)
    assert spy.call_count == 1
    assert broker.get_portfolio().btc_quantity == original.btc_quantity
    report = db.report()
    assert report["fill_count"] == 1
    assert report["recorded_fees_usd"] > 0
    assert report["ledger_matches_position"]


@pytest.mark.parametrize("expired", [False, True])
def test_preflight_read_delay_rechecks_temporal_risk_before_claim(setup, monkeypatch, expired):
    settings, db, broker = setup
    saved = preview(setup)
    now = utcnow()
    if expired:
        decision = replace(db.get_run(saved["run_id"])["decision"], expires_at=now + timedelta(seconds=1))
        with db.connection:
            db.connection.execute(
                "UPDATE decisions SET body=? WHERE run_id=?", (dumps(decision), saved["run_id"])
            )
    monkeypatch.setattr("crypto_agent.risk.checks.utcnow", lambda: now)
    original_lookup = broker.get_order

    def delayed_lookup(client_order_id):
        nonlocal now
        result = original_lookup(client_order_id)
        now += timedelta(seconds=2 if expired else 61)
        return result

    broker.get_order = delayed_lookup
    broker.submit_order = Mock(wraps=broker.submit_order)
    result = execute_preview(settings, broker, db, saved["run_id"], True)
    assert result["status"] == "blocked"
    assert result["risk"].reasons == (("Decision is expired" if expired else "Market data is stale"),)
    assert db.order_for_run(saved["run_id"])["attempted"] == 0
    assert db.get_run(saved["run_id"])["status"] == "blocked"
    assert (
        db.connection.execute(
            "SELECT count(*) FROM risk_results WHERE run_id=? AND phase='pre_submit'", (saved["run_id"],)
        ).fetchone()[0]
        == 1
    )
    broker.submit_order.assert_not_called()


def test_preflight_account_snapshot_is_revalidated_before_claim(setup):
    settings, db, broker = setup
    saved = preview(setup)
    original_portfolio = broker.get_portfolio
    calls = 0

    def account_reads():
        nonlocal calls
        calls += 1
        portfolio = original_portfolio()
        # The last identity check is after execute_preview's initial risk check.
        return (
            replace(portfolio, observed_at=portfolio.observed_at - timedelta(seconds=61))
            if calls == 3
            else portfolio
        )

    broker.get_portfolio = account_reads
    broker.submit_order = Mock(wraps=broker.submit_order)
    result = execute_preview(settings, broker, db, saved["run_id"], True)
    assert result["status"] == "blocked"
    assert result["risk"].reasons == ("Account data is stale",)
    assert db.order_for_run(saved["run_id"])["attempted"] == 0
    broker.submit_order.assert_not_called()


def test_accepted_then_timeout_is_queried_not_retried(setup):
    settings, db, broker = setup
    result = preview(setup)
    submit = broker.submit_order

    def timeout_after_accept(order):
        submit(order)
        raise SubmissionUnknown("network timeout")

    spy = Mock(side_effect=timeout_after_accept)
    broker.submit_order = spy
    execution = execute_preview(settings, broker, db, result["run_id"], True)
    assert execution["execution"]["recovered_after_uncertainty"]
    execute_preview(settings, broker, db, result["run_id"], True)
    assert spy.call_count == 1
    assert db.report()["fill_count"] == 1


def test_timeout_before_accept_never_blindly_resubmits(setup):
    settings, db, broker = setup
    result = preview(setup)
    broker.submit_order = Mock(side_effect=SubmissionUnknown("unknown"))
    execute_preview(settings, broker, db, result["run_id"], True)
    assert db.order_for_run(result["run_id"])["status"] == "unknown"
    execute_preview(settings, broker, db, result["run_id"], True)
    assert broker.submit_order.call_count == 1
    with pytest.raises(AgentError, match="uncertain"):
        run_once(settings, broker, db)


def test_partial_fill_then_restart_and_reconcile(setup):
    settings, db, broker = setup
    broker.fill_fraction = Decimal("0.5")
    result = preview(setup)
    execute_preview(settings, broker, db, result["run_id"], True)
    partial = broker.get_order(result["order"].client_order_id)
    assert partial.status == "partially_filled"
    assert db.report()["fill_count"] == 1
    # Fresh connection simulates a new process; broker state is also persisted.
    restarted_db = Database(settings.database_path, "offline")
    broker.fill_order(result["order"].client_order_id)
    reconcile_account(broker, restarted_db)
    assert restarted_db.order_for_run(result["run_id"])["status"] == "filled"
    assert restarted_db.report()["fill_count"] == 2
    assert restarted_db.report()["ledger_matches_position"]
    restarted_db.close()


def test_reconciliation_request_count_does_not_grow_with_terminal_history(setup):
    settings, db, broker = setup
    saved = preview(setup)
    execute_preview(settings, broker, db, saved["run_id"], True)
    terminal = broker.get_order(saved["order"].client_order_id)
    # Synthetic long history: each row previously generated a GET on every check.
    for i in range(250):
        run_id, client_id = f"history-run-{i}", f"history-order-{i}"
        db.create_run(run_id, settings.mode, settings.digest, settings.summary)
        db.preview(run_id, replace(saved["order"], client_order_id=client_id))
        db.claim_submission(client_id)
        db.order_state(client_id, "filled", replace(terminal, client_order_id=client_id))
    broker.get_order = Mock(wraps=broker.get_order)
    broker.get_portfolio = Mock(wraps=broker.get_portfolio)
    broker.get_activities = Mock(wraps=broker.get_activities)
    result = reconcile_account(broker, db)
    broker.get_order.assert_not_called()
    assert result["terminal_orders_skipped"] == 251
    assert result["orders"] == []
    assert broker.get_portfolio.call_count == 2
    broker.get_activities.assert_called_once()
    assert result["portfolio"].btc_quantity > 0


def test_terminal_orders_can_still_be_explicitly_audited(setup):
    settings, db, broker = setup
    saved = preview(setup)
    execute_preview(settings, broker, db, saved["run_id"], True)
    broker.get_order = Mock(wraps=broker.get_order)
    result = reconcile_account(broker, db, refresh_terminal=True)
    broker.get_order.assert_called_once_with(saved["order"].client_order_id)
    assert result["terminal_orders_skipped"] == 0
    assert result["orders"][0]["order"].status == "filled"


@pytest.mark.parametrize("state", ["unknown", "submitting", "partially_filled", "pending_cancel"])
def test_nonterminal_orders_always_get_fresh_broker_lookup(setup, state):
    settings, db, broker = setup
    saved = preview(setup)
    execute_preview(settings, broker, db, saved["run_id"], True)
    db.order_state(saved["order"].client_order_id, state)
    broker.get_order = Mock(wraps=broker.get_order)
    result = reconcile_account(broker, db)
    broker.get_order.assert_called_once_with(saved["order"].client_order_id)
    assert result["terminal_orders_skipped"] == 0
    assert db.order_for_run(saved["run_id"])["status"] == "filled"


def test_crash_after_claim_before_post_is_quarantined_on_restart(setup):
    settings, db, broker = setup
    result = preview(setup)
    assert db.claim_submission(result["order"].client_order_id)
    restarted_db = Database(settings.database_path, "offline")
    broker.submit_order = Mock(wraps=broker.submit_order)
    execute_preview(settings, broker, restarted_db, result["run_id"], True)
    assert restarted_db.order_for_run(result["run_id"])["status"] == "unknown"
    broker.submit_order.assert_not_called()
    restarted_db.close()


def test_rejection_saved_and_not_retried(setup):
    settings, db, broker = setup
    result = preview(setup)
    broker.submit_order = Mock(side_effect=OrderRejected("rejected"))
    execute_preview(settings, broker, db, result["run_id"], True)
    assert db.order_for_run(result["run_id"])["status"] == "rejected"
    execute_preview(settings, broker, db, result["run_id"], True)
    assert broker.submit_order.call_count == 1


def test_cancel_partial_preserves_fills(setup):
    settings, db, broker = setup
    broker.fill_fraction = Decimal("0.5")
    result = preview(setup)
    execute_preview(settings, broker, db, result["run_id"], True)
    from crypto_agent.runner import cancel_order

    cancellation = cancel_order(broker, db, result["order"].client_order_id, True)
    assert cancellation["portfolio"].open_orders == ()
    assert db.order_for_run(result["run_id"])["status"] == "canceled"
    assert db.report()["fill_count"] == 1
    assert db.report()["ledger_matches_position"]


def test_unpreviewed_order_and_changed_config_are_rejected(setup):
    settings, db, broker = setup
    result = preview(setup)
    changed = replace(result["order"], quantity=result["order"].quantity + Decimal("0.001"))
    with pytest.raises(AgentError, match="unchanged persisted"):
        execute_order(changed, broker, db)
    settings = replace(settings, risk={**settings.risk, "max_daily_loss_usd": Decimal(101)})
    with pytest.raises(AgentError, match="Configuration"):
        execute_preview(settings, broker, db, result["run_id"], True)


def test_db_bound_to_environment_and_account(setup):
    settings, db, broker = setup
    preview(setup)
    with pytest.raises(AgentError, match="mode mismatch"):
        Database(settings.database_path, "paper")
    with pytest.raises(AgentError, match="account_id mismatch"):
        db.bind("account_id", "different-account")


def test_hold_does_not_submit(setup):
    settings, db, broker = setup
    from crypto_agent.strategies.baseline import BaselineStrategy

    decision = BaselineStrategy(settings.strategy).decide(broker.get_market(), broker.get_portfolio())
    decision = replace(decision, rating="Hold", actionable=False, target_position_pct=Decimal(0))
    strategy = Mock()
    strategy.decide.return_value = decision
    result = run_once(settings, broker, db, strategy=strategy)
    assert result["status"] == "no_order", result
    assert not db.orders()


def test_review_is_a_normal_no_order_outcome(setup):
    settings, db, broker = setup
    from crypto_agent.strategies.baseline import BaselineStrategy

    decision = BaselineStrategy(settings.strategy).decide(broker.get_market(), broker.get_portfolio())
    decision = replace(
        decision,
        rating="REVIEW",
        actionable=False,
        target_position_pct=None,
        reason="Configured strategy gate requested review",
    )
    strategy = Mock()
    strategy.decide.return_value = decision
    result = run_once(settings, broker, db, strategy=strategy)
    assert result["status"] == "no_order", result
    assert result["risk"].allowed is False
    assert not db.orders()


def test_paper_cannot_be_enabled_by_offline_explicit_flag(setup):
    settings, db, broker = setup
    result = preview(setup)
    paper_settings = replace(settings, mode="paper")
    with pytest.raises(AgentError, match="disabled"):
        execute_preview(paper_settings, broker, db, result["run_id"], True)


def test_forged_broker_identity_remains_unknown(setup):
    settings, db, broker = setup
    result = preview(setup)
    submit = broker.submit_order
    broker.submit_order = lambda order: replace(submit(order), client_order_id="mismatch")
    with pytest.raises(AgentError, match="identity"):
        execute_preview(settings, broker, db, result["run_id"], True)
    assert db.order_for_run(result["run_id"])["status"] == "unknown"


def test_daily_baseline_survives_restart(setup):
    settings, db, broker = setup
    preview(setup)
    p, m = broker.get_portfolio(), broker.get_market()
    db.snapshot(replace(p, equity_usd=Decimal(9900)), m)
    restarted = Database(settings.database_path, "offline")
    assert restarted.daily_baseline(p.observed_at) == 10000
    restarted.close()


def test_wrong_account_reconcile_and_cancel_do_not_mutate(setup, tmp_path):
    settings, db, broker = setup
    broker.fill_fraction = Decimal(0)
    result = preview(setup)
    execute_preview(settings, broker, db, result["run_id"], True)
    original_rows = db.orders()
    other = OfflineBroker(tmp_path / "other-broker.sqlite")
    other.cancel_order = Mock()
    from crypto_agent.runner import cancel_order

    with pytest.raises(AgentError, match="account_id mismatch"):
        reconcile_account(other, db)
    with pytest.raises(AgentError, match="account_id mismatch"):
        cancel_order(other, db, result["order"].client_order_id, True)
    other.cancel_order.assert_not_called()
    assert db.orders() == original_rows
    other.close()


def test_conflicting_limit_from_broker_is_not_accepted(setup):
    settings, db, broker = setup
    result = preview(setup)
    submit = broker.submit_order
    broker.submit_order = lambda order: replace(submit(order), limit_price=order.limit_price + 1)
    with pytest.raises(AgentError, match="identity"):
        execute_preview(settings, broker, db, result["run_id"], True)
    assert db.order_for_run(result["run_id"])["status"] == "unknown"


def test_concurrent_process_lock_rejects_second_owner(setup):
    settings, db, broker = setup
    other_db = Database(settings.database_path, "offline")
    with db.lock():
        with pytest.raises(AgentError, match="Another process"):
            with other_db.lock():
                pytest.fail("Second writer acquired lock")
    other_db.close()


def test_report_roundtrip_sell_realized_and_fees(setup):
    settings, db, broker = setup
    buy = preview(setup)
    execute_preview(settings, broker, db, buy["run_id"], True)
    sell_settings = replace(
        settings, strategy={**settings.strategy, "baseline_buy_below_usd": Decimal(40000)}
    )
    sell = run_once(sell_settings, broker, db)
    assert sell["order"].side == "sell"
    execute_preview(sell_settings, broker, db, sell["run_id"], True)
    report = db.report()
    assert report["fill_count"] == 2
    assert report["ledger_matches_position"]
    assert report["realized_pnl_gross_usd"] < 0
    assert report["unrealized_pnl_usd"] == 0
    assert (
        report["observed_equity_change_usd"] == report["realized_pnl_gross_usd"] - report["recorded_fees_usd"]
    )


def test_date_only_fees_mark_net_attribution_uncertain(setup):
    settings, db, broker = setup
    result = preview(setup)
    execute_preview(settings, broker, db, result["run_id"], True)
    from crypto_agent.models import Activity, utcnow

    db.activities(
        [
            Activity(
                "date-fee",
                "FEE",
                utcnow().replace(hour=0, minute=0, second=0),
                fee_usd=Decimal("2"),
                time_precision="day",
            )
        ]
    )
    report = db.report()
    assert report["fee_attribution_uncertain"]
    assert report["realized_minus_recorded_fees_usd"] is None


def test_serializer_redacts_local_credentials(monkeypatch):
    from crypto_agent.models import dumps

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-private-api-key")
    assert "synthetic-private-api-key" not in dumps({"reason": "Echo synthetic-private-api-key"})


def test_safe_saved_preview_survives_small_quote_rounding_change(setup):
    settings, db, broker = setup
    result = preview(setup)
    market = broker.get_market
    broker.get_market = lambda: replace(
        market(), price=Decimal(50001), bid=Decimal(49991), ask=Decimal(50011)
    )
    execution = execute_preview(settings, broker, db, result["run_id"], True)
    assert execution["execution"]["order"].quantity == result["order"].quantity
    assert execution["execution"]["order"].limit_price == result["order"].limit_price


def test_saved_preview_rejected_when_quote_moves_outside_reviewed_bounds(setup):
    settings, db, broker = setup
    result = preview(setup)
    market = broker.get_market
    broker.get_market = lambda: replace(
        market(), price=Decimal(52000), bid=Decimal(51990), ask=Decimal(52010)
    )
    broker.submit_order = Mock(wraps=broker.submit_order)
    execution = execute_preview(settings, broker, db, result["run_id"], True)
    assert execution["status"] == "blocked"
    broker.submit_order.assert_not_called()


def probe_setup(setup):
    from crypto_agent.strategies.baseline import BaselineStrategy

    settings, db, broker = setup
    strategy = BaselineStrategy(settings.strategy)
    decide = strategy.decide
    strategy.decide = lambda market, portfolio: replace(
        decide(market, portfolio),
        rating="Buy",
        target_position_pct=Decimal(10) / portfolio.equity_usd,
    )
    config = {
        **settings.strategy,
        "name": "intraday_ai",
        "intraday_entry_policy": "capped_probe",
        "intraday_probe_max_position_usd": Decimal(10),
        "intraday_probe_cost_budget_usd": Decimal(".105"),
    }
    return replace(settings, strategy=config), db, broker, strategy


def test_offline_probe_closed_loop_caps_total_and_does_not_repeat(setup):
    settings, db, broker, strategy = probe_setup(setup)
    preview = run_once(settings, broker, db, strategy=strategy)
    assert preview["status"] == "preview"
    assert preview["order"].estimated_notional_usd <= 10
    execute_preview(settings, broker, db, preview["run_id"], True)
    execute_preview(settings, broker, db, preview["run_id"], True)
    assert db.report()["fill_count"] == 1
    assert run_once(settings, broker, db, strategy=strategy)["status"] == "no_order"


@pytest.mark.parametrize("phase", ["preview", "execute"])
def test_probe_budget_rechecked_at_preview_and_submission(setup, phase):
    settings, db, broker, strategy = probe_setup(setup)
    if phase == "execute":
        preview = run_once(settings, broker, db, strategy=strategy)
        assert preview["status"] == "preview"
    market = broker.get_market
    broker.get_market = lambda *args: replace(market(*args), bid=Decimal(49960))
    broker.submit_order = Mock(side_effect=AssertionError("must not POST"))
    if phase == "preview":
        result = run_once(settings, broker, db, strategy=strategy)
        assert result["status"] == "blocked"
        assert "probe" in result["risk"].reasons[0]
    else:
        result = execute_preview(settings, broker, db, preview["run_id"], True)
        assert result["status"] == "blocked"
        assert "probe" in result["risk"].reasons[0]
    broker.submit_order.assert_not_called()


def test_retry_delay_cannot_promote_a_previously_open_bar_to_fresh_evidence(setup, monkeypatch):
    settings, db, broker = setup
    settings = replace(settings, strategy={**settings.strategy, "intraday_max_bar_age_seconds": 180})
    saved = run_once(settings, broker, db)
    assert saved["status"] == "preview"
    decision = db.get_run(saved["run_id"])["decision"]
    now = decision.created_at
    # Only the first bar was closed at decision time. The second closes during the delay.
    bars = [{"observed_at": now - timedelta(seconds=235)}, {"observed_at": now - timedelta(seconds=55)}]
    with db.connection:
        db.connection.execute("INSERT INTO intraday_contexts VALUES (?,?)", (saved["run_id"], dumps(bars)))
    monkeypatch.setattr("crypto_agent.runner.utcnow", lambda: now)
    original_lookup = broker.get_order

    def delayed_lookup(client_order_id):
        nonlocal now
        value = original_lookup(client_order_id)
        now += timedelta(seconds=10)
        return value

    broker.get_order = delayed_lookup
    broker.submit_order = Mock(wraps=broker.submit_order)
    result = execute_preview(settings, broker, db, saved["run_id"], True)
    assert result["status"] == "blocked"
    assert result["risk"].reasons == ("Intraday minute bars are stale",)
    assert db.order_for_run(saved["run_id"])["attempted"] == 0
    broker.submit_order.assert_not_called()
