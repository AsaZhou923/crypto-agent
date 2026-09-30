# Local monitor API v1

All amounts and ratios are decimal strings; missing measurements are null. Timestamps are timezone-aware ISO 8601. Percent fields are fractions (0.01 means 1%). `mode` is `demo` only with explicit `--demo`; Paper failures never substitute demo data.

- `GET /api/dashboard?refresh=true&scenario=normal|stale|partial|empty|disconnected`
- `GET /api/market?symbol=BTC/USD|XRP/USD&timeframe=1Min|5Min|1Hour&refresh=true&scenario=...` (symbol defaults to BTC/USD)

`scenario` is supported only in demo. Dashboard: `{mode,as_of,timezone:'Asia/Tokyo',sections:{account,ledger,agent,equity}}`. Market returns a Section.

Section: `{source,as_of,stale,error,data,notice?}`. Successful data is retained on fetch failure and marked stale; `as_of` describes the observation, not a failed refresh.

Paper market data includes `checked_at`, the last successful upstream query time. Section `as_of` remains the latest bar start time. A successful query can still return delayed bars (`stale=true`, `error=null`); the UI distinguishes this source delay from a failed request. Failed refreshes retain both prior timestamps.

Account: `{equity,cash,buying_power,positions:[{symbol,quantity,average_entry_price,current_price,market_value,unrealized_pnl}],metrics:{daily,total,drawdown}}`; Metric: `{value,percent,basis}`.
Ledger: `{orders:[{id,client_order_id,symbol,side,quantity,filled_quantity,filled_avg_price,status,submitted_at,run_id}],fills:[{id,order_id,symbol,side,quantity,price,fee,occurred_at,run_id}],fees:[{id,order_id,amount,currency,symbol,attribution,occurred_at}],notice}`.
Agent: `{decisions:[{run_id,symbol,created_at,expires_at,rating,strategy_version,model,target_position_pct,current_position_pct,reason,evidence,risk:[{phase,allowed,reasons,created_at}],order_ids,raw}],logs:[{id,run_id,phase,status,started_at,duration_ms,error}],status,last_run_at,notice}`.
Equity: `{points:[{time,value}],basis}`. Market: `{symbol:'BTC/USD'|'XRP/USD',timeframe,bars:[{time,open,high,low,close,volume}],notice}`. Market requests, caches and failed-refresh retention are isolated by both symbol and timeframe. Unsupported symbols/timeframes return HTTP 422. Demo XRP bars are independent deterministic synthetic data, with no invented XRP trades or decisions.

Read-only projections plus the fixed scheduler control below; loopback Host/Origin, fixed Alpaca Paper broker, explicit account identity check for SQLite association. Read-only SQLite connections do not create or migrate files. API excludes saved config/environment. Existing `models.dumps` redacts credential values before serialization. TTLs: account/ledger/equity/market 8 s, agent 3 s, performance 60 s. UI polls dashboard every 5 s and the selected market every 10 s; TTL headroom allows normal account refresh around 10 s and Agent refresh around 5 s. In-flight polling requests are skipped, not queued. Concurrent requests share one refresh, including manually requested refreshes. Manual refreshes within 2 s reuse the last attempt.

Paper performance uses closed 1Min continuous Alpaca portfolio-history buckets plus an ALL-activity funding audit, cached for 60 seconds. Timestamps label bucket starts, equity labels bucket ends; daily baseline is Tokyo midnight. Total/drawdown cover the displayed tracking interval, capped at 29 days. Minute history is queried in at most 6-day windows because Alpaca rejects 1Min intervals longer than 7 days; points belong to (start, end] by bucket end, without duplicate funding at boundaries. CSD/CSW/ACATC are external capital; unknown transfers, mismatches and gaps return specific unavailable reasons. Failed reads retain stale metric values independently of the account cache. Demo uses a fixed 2026-09-19T12:00:00Z clock and known initial funding/transactions, including cash-flow-aware Decimal calculations. Equity curve is observed account value, not BTC price or cash-flow-adjusted return.

Upstream references verified 2026-09-19: https://docs.alpaca.markets/us/reference/getallorders-1 (500 orders maximum, status=all); https://docs.alpaca.markets/us/reference/cryptobars-1 (1Min/5Min/1Hour, US location, descending latest page). Activity reads reuse the existing paginated Alpaca broker adapter.

Agent freshness uses the latest persisted run creation or cycle event timestamp (completed cycles use `ended_at`, unfinished cycles `started_at`), not database fetch time. More than 1,800 seconds since the latest recorded event marks the agent section stale. Up to 500 `auto_cycles` are included as phase `auto_cycle`; duration is the nonnegative actual `ended_at - started_at` in milliseconds, otherwise null. Only common textual cycle reason/message/error/warning fields are projected; no cycle config/raw payload. A missing database explicitly errors without creating it. Corrupt order-association records degrade to unlinked platform ledger data with a notice.

Equity merges account-matched SQLite history with actual successful Paper account observations collected by the monitor. Samples are bounded in memory, cleared on account change, never written to the trading database. Historical timestamps are preserved; only new real observations advance freshness. Browser timers continue automatically and refresh immediately on visibility/focus/network recovery.


## Fixed local scheduler control

Linux deployment also supports the fixed user units `crypto-agent-paper.timer` and `.service`, with exact installed unit validation, no drop-ins, and a loaded-config freshness check. Linux stop persists the pause marker before disabling the timer and stopping the active service. Start never directly launches a tick; each completed invocation is followed by a 300-second delay. macOS retains launchd behavior.

The API always binds loopback. An explicit `--external-origin https://<host>.ts.net:<port>` permits that exact external Host/Origin behind Tailscale Serve; no wildcard is accepted. Control requests must match that origin exactly and include the control header. The network authorization boundary is Tailscale Serve and the existing tailnet ACL.

`GET /api/health/scheduler` returns `{status:"ok"|"degraded",reason,last_started_at?,last_checked_at?}` with HTTP 200/503. Health includes the enabled state, active timer, approval checks, unknown submissions and scheduler heartbeat freshness (1,260 seconds). Read-only checks do not refresh the heartbeat; valid broker retry cooldowns do not suppress scheduler check heartbeats.

- `GET /api/scheduler`: `{available,enabled,loaded,running,can_start,can_stop,state,reason,interval_seconds,symbols,as_of,error}`. Polling reads actual launchd state and SQLite through a read-only connection. `enabled` is the trading authorization after the pause marker; `loaded` and `running` separately describe launchd.
- `POST /api/scheduler`: JSON `{action:"start"|"stop"}`, no extra fields. Requires exact same-origin `Origin` and `X-Crypto-Agent-Control: 1`. No arbitrary command, path, label, symbol or mode accepted. Other mutations remain HTTP 405. Demo and other project configurations cannot control the fixed Paper task.
- Start is bounded and does not run a tick or kickstart. Stop persists the pause marker before unloading launchd; accepted platform orders are not canceled. Failures return safe HTTP 409/503 detail, and the UI reads status again without retrying the write.
- `GET /api/health` has `read_only:false` only when this monitor supports the fixed local scheduler controller.
