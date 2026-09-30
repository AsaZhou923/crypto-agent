"""Read-only Paper projections and bounded single-flight caches."""

import os
import threading
import time
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from crypto_agent.api import demo
from crypto_agent.api.database import agent_view, equity_view, order_links, read_database
from crypto_agent.api.performance import load_performance, unavailable_metrics
from crypto_agent.brokers.alpaca_paper import PAPER_URL, AlpacaPaperBroker, parse_order
from crypto_agent.config import load_settings
from crypto_agent.models import AgentError, decimal, normalize_symbol, timestamp, utcnow


class Cache:
    def __init__(self):
        self.entries = {}
        self.locks = {}
        self.guard = threading.Lock()

    def get(self, key, loader, *, source, ttl, refresh=False):
        requested = time.monotonic()
        with self.guard:
            lock = self.locks.setdefault(key, threading.Lock())
        with lock:
            entry = self.entries.get(key)
            now = time.monotonic()
            # Waiting callers reuse an attempt completed after their arrival.
            if entry and (entry[0] >= requested or now - entry[0] < (2 if refresh else ttl)):
                return deepcopy(entry[1])
            try:
                data, observed, notice, stale = loader()
                value = {
                    "source": source,
                    "as_of": observed,
                    "stale": stale,
                    "error": None,
                    "data": data,
                    "notice": notice,
                }
            except Exception as exc:
                error = (
                    str(exc)
                    if isinstance(exc, AgentError)
                    else "数据读取失败；检查本地配置、数据库或平台响应。"
                )
                value = (
                    deepcopy(entry[1])
                    if entry
                    else {"source": source, "as_of": None, "data": None, "notice": "未自动切换演示数据。"}
                )
                value.update(stale=True, error=error)
            self.entries[key] = (time.monotonic(), value)
            return deepcopy(value)


class Monitor:
    def __init__(self, *, demo_mode=False, config_dir=Path("config"), root=None, broker=None):
        self.demo_mode = demo_mode
        self.root = (root or Path.cwd()).resolve()
        self.cache = Cache()
        self.broker = broker
        self.settings = None
        self.config_error = None
        self.account_id = None
        self.performance_day = None
        self.equity_samples = {}
        self.network_lock = threading.RLock()
        if not demo_mode:
            try:
                self.settings = load_settings(Path(config_dir), root=self.root)
                if not self.broker:
                    self.broker = AlpacaPaperBroker(
                        PAPER_URL,
                        os.environ.get("ALPACA_API_KEY", ""),
                        os.environ.get("ALPACA_SECRET_KEY", ""),
                        timeout_seconds=self.settings.paper["request_timeout_seconds"],
                        allow_submit=False,
                        symbols=tuple(self.settings.paper["symbols"]),
                    )
            except AgentError as exc:
                self.config_error = str(exc)

    def close(self):
        if self.broker:
            self.broker.close()

    def require_broker(self):
        if not self.broker:
            raise AgentError(self.config_error or "缺少本地 Alpaca Paper 凭据。")
        return self.broker

    def database(self):
        if not self.settings:
            raise AgentError(self.config_error or "本地配置不可用。")
        return read_database(self.settings.database_path)

    def account(self):
        broker = self.require_broker()
        with self.network_lock:
            portfolio = broker.get_portfolio()
            positions_raw = broker._request("GET", "/v2/positions")
        positions = []
        for row in positions_raw:
            positions.append(
                {
                    "symbol": normalize_symbol(row["symbol"]),
                    "quantity": str(decimal(row["qty"])),
                    "average_entry_price": str(decimal(row["avg_entry_price"])),
                    "current_price": str(decimal(row["current_price"])),
                    "market_value": str(decimal(row["market_value"])),
                    "unrealized_pnl": str(decimal(row["unrealized_pl"])),
                }
            )
        metrics = unavailable_metrics("收益历史尚未读取。")
        value = {
            "equity": str(portfolio.equity_usd),
            "cash": str(portfolio.cash_usd),
            "buying_power": str(portfolio.buying_power_usd),
            "positions": positions,
            "metrics": metrics,
        }
        # These are actual successful Paper observations, not inferred history.
        # Keep them in monitor memory only; never write to the trading ledger.
        with self.network_lock:
            if self.account_id != portfolio.account_id:
                self.equity_samples.clear()
            self.account_id = portfolio.account_id
            self.equity_samples[portfolio.observed_at.isoformat()] = str(portfolio.equity_usd)
            while len(self.equity_samples) > 2000:
                del self.equity_samples[next(iter(self.equity_samples))]
        return (
            value,
            portfolio.observed_at.isoformat(),
            "Alpaca Paper 账户与持仓为顺序 GET 观察，非原子快照；USD。",
            False,
        )

    def ledger(self):
        broker = self.require_broker()
        observed = utcnow()
        with self.network_lock:
            raw_orders = broker._request(
                "GET", "/v2/orders", params={"status": "all", "limit": 500, "direction": "desc"}
            )
            activities = broker.get_activities(after=observed - timedelta(days=30))
        try:
            clients, brokers = order_links(self.database(), self.account_id)
            association_notice = (
                "关联仅按账户绑定及保存的 client_order_id / broker_order_id；无 run_id 表示未能证明关联。"
            )
        except (AgentError, ValueError, TypeError, KeyError):
            clients, brokers = {}, {}
            association_notice = (
                "本地账本不可读或关联记录损坏，平台订单和成交不关联决策；平台流水仍正常展示。"
            )
        orders, fills, fees = [], [], []
        for row in raw_orders:
            parsed = parse_order(row)
            linked = clients.get(parsed.client_order_id) or brokers.get(parsed.broker_order_id)
            if linked:
                brokers[parsed.broker_order_id] = linked
            orders.append(
                {
                    "id": parsed.broker_order_id,
                    "client_order_id": parsed.client_order_id,
                    "symbol": parsed.symbol,
                    "side": parsed.side,
                    "quantity": str(parsed.quantity),
                    "filled_quantity": str(parsed.filled_quantity),
                    "filled_avg_price": str(parsed.filled_avg_price)
                    if parsed.filled_avg_price is not None
                    else None,
                    "status": parsed.status,
                    "submitted_at": row.get("submitted_at"),
                    "run_id": linked,
                }
            )
        for a in activities:
            if a.kind == "FILL":
                fills.append(
                    {
                        "id": a.activity_id,
                        "order_id": a.order_id,
                        "symbol": a.symbol,
                        "side": a.side,
                        "quantity": str(a.quantity),
                        "price": str(a.price),
                        "fee": None,
                        "occurred_at": a.occurred_at.isoformat(),
                        "run_id": brokers.get(a.order_id),
                    }
                )
            else:
                attribution = (
                    "asset fee attributed to symbol"
                    if a.kind == "CFEE" and a.symbol and a.quantity < 0
                    else "USD fee / coin attribution unavailable"
                    if a.currency == "USD"
                    else "coin attribution unavailable"
                )
                fees.append(
                    {
                        "id": a.activity_id,
                        "order_id": a.order_id,
                        "amount": str(a.fee_usd) if a.fee_usd is not None else None,
                        "currency": a.currency,
                        "symbol": a.symbol,
                        "attribution": attribution,
                        "occurred_at": a.occurred_at.date().isoformat()
                        if a.time_precision == "day"
                        else a.occurred_at.isoformat(),
                    }
                )
        notice = (
            "平台最近最多 500 笔订单；FILL/CFEE/FEE 为最近 30 天（首日费用按日期精度）。逐笔费用未直接归属时显示未知，单列费用可能延迟入账。"
            + association_notice
        )
        if len(raw_orders) >= 500:
            notice += "订单已到 500 条上限，更早记录未展示。"
        value = {
            "orders": orders,
            "fills": sorted(fills, key=lambda row: row["occurred_at"], reverse=True),
            "fees": fees,
            "notice": notice,
        }
        return value, observed.isoformat(), notice, False

    def agent(self):
        value = agent_view(self.database(), self.account_id)
        cached_ledger = self.cache.entries.get("ledger")
        ledger = cached_ledger[1].get("data") if cached_ledger else None
        if ledger:
            for decision in value["decisions"]:
                linked = [order["id"] for order in ledger["orders"] if order["run_id"] == decision["run_id"]]
                decision["order_ids"] = sorted(set(decision["order_ids"] + linked))
        at = value["last_run_at"]
        stale = bool(at and (utcnow() - timestamp(at)).total_seconds() > 1800)
        if stale:
            value["notice"] += "最近运行记录已超过 30 分钟，请核查实际调度器或进程。"
        return value, at, value["notice"], stale

    def equity(self):
        with self.network_lock:
            account_id = self.account_id
            samples = dict(self.equity_samples)
        try:
            value = equity_view(self.database(), account_id)
        except AgentError as exc:
            if not samples:
                raise
            value = {"points": [], "basis": "本地历史暂不可用：" + str(exc)}
        points = {point["time"]: point["value"] for point in value["points"]}
        points.update(samples)
        value["points"] = [{"time": at, "value": points[at]} for at in sorted(points)[-2000:]]
        value["basis"] += (
            "曲线实时接入监控台每次成功读取的 Alpaca Paper 权益，账户约每 10 秒更新；"
            "只连接实际观察点，中间时刻没有采样。新增点仅保留在服务内存，重启后重新采集，不写交易账本。"
        )
        at = value["points"][-1]["time"] if value["points"] else None
        return value, at, value["basis"], bool(at and (utcnow() - timestamp(at)).total_seconds() > 120)

    def performance(self):
        tracking_start = None
        try:
            metadata = self.database()["metadata"]
            if metadata.get("account_id") == self.account_id:
                tracking_start = metadata.get("tracking_started_at")
        except AgentError:
            pass  # The platform's own history remains authoritative.
        with self.network_lock:
            metrics = load_performance(self.require_broker(), utcnow(), tracking_start)
        return metrics, utcnow().isoformat(), "Alpaca 权益历史和完整区间活动核对。", False

    def dashboard(self, refresh=False, scenario="normal"):
        if self.demo_mode:
            return demo.scenario_response(demo.dashboard(), scenario)
        sections = {}
        # Cache TTLs leave room for request latency within the 5s UI poll.
        for name, source, ttl in [
            ("account", "Alpaca Paper", 8),
            ("ledger", "Alpaca Paper", 8),
            ("agent", "本地 Paper SQLite", 3),
            ("equity", "Alpaca Paper / 本地历史", 8),
        ]:
            sections[name] = self.cache.get(
                name, getattr(self, name), source=source, ttl=ttl, refresh=refresh
            )
        if sections["account"]["data"] is not None:
            day = utcnow().astimezone(ZoneInfo("Asia/Tokyo")).date()
            if day != self.performance_day:
                self.cache.entries.pop("performance", None)
                self.performance_day = day
            stats = self.cache.get(
                "performance", self.performance, source="Alpaca Paper 权益历史", ttl=60, refresh=refresh
            )
            metrics = stats["data"] or unavailable_metrics(stats["error"] or "收益历史暂不可用。")
            for metric in metrics.values():
                if stats["stale"]:
                    metric.update(stale=True, error=stats["error"], subtitle="统计更新失败；保留上次成功值。")
                    metric["basis"] += " 最新读取错误：" + (stats["error"] or "未知")
            sections["account"]["data"]["metrics"] = metrics
        return {
            "mode": "paper",
            "as_of": utcnow().isoformat(),
            "timezone": "Asia/Tokyo",
            "sections": sections,
        }

    def market(self, timeframe="1Min", refresh=False, scenario="normal", *, symbol="BTC/USD"):
        if symbol not in {"BTC/USD", "XRP/USD"}:
            raise AgentError("行情仅支持 BTC/USD、XRP/USD。")
        if timeframe not in {"1Min", "5Min", "1Hour"}:
            raise AgentError("行情周期仅支持 1Min、5Min、1Hour。")
        if self.demo_mode:
            return demo.scenario_response(demo.market(timeframe, symbol=symbol), scenario, is_market=True)

        def fetch():
            broker = self.require_broker()
            now = utcnow()
            minutes = {"1Min": 1, "5Min": 5, "1Hour": 60}[timeframe]
            with self.network_lock:
                raw, page_token, seen_tokens = [], None, set()
                for _ in range(10):
                    params = {
                        "symbols": symbol,
                        "timeframe": timeframe,
                        "start": (now - timedelta(minutes=minutes * 400)).isoformat(),
                        "end": now.isoformat(),
                        "sort": "desc",
                        "limit": 180,
                    }
                    if page_token:
                        params["page_token"] = page_token
                    payload = broker._request("GET", "/v1beta3/crypto/us/bars", data=True, params=params)
                    page = payload["bars"].get(symbol, [])
                    if not isinstance(page, list):
                        raise AgentError("Alpaca K 线响应无效。")
                    raw.extend(page)
                    page_token = payload.get("next_page_token")
                    if len(raw) >= 180 or not page_token:
                        break
                    if page_token in seen_tokens:
                        raise AgentError("Alpaca K 线分页不完整。")
                    seen_tokens.add(page_token)
            bars = []
            for row in raw[:180]:
                values = {
                    key: decimal(row[source])
                    for key, source in [
                        ("open", "o"),
                        ("high", "h"),
                        ("low", "l"),
                        ("close", "c"),
                        ("volume", "v"),
                    ]
                }
                if (
                    min(values[k] for k in ["open", "high", "low", "close"]) <= 0
                    or values["volume"] < 0
                    or values["low"] > min(values["open"], values["close"])
                    or values["high"] < max(values["open"], values["close"])
                ):
                    raise AgentError("Alpaca K 线数值无效。")
                bars.append(
                    {"time": timestamp(row["t"]).isoformat(), **{key: str(v) for key, v in values.items()}}
                )
            bars.sort(key=lambda bar: bar["time"])
            if len({bar["time"] for bar in bars}) != len(bars):
                raise AgentError("Alpaca K 线包含重复时间。")
            at = bars[-1]["time"] if bars else None
            stale = bool(at and (now - timestamp(at)).total_seconds() > minutes * 60 + 120)
            notice = "Alpaca crypto US bars；最近最多 180 根，末根可能尚未闭合；行情时间以柱起点为准，成交来自 Paper 执行流水。"
            if page_token and len(raw) >= 180:
                notice += "更早 K 线未加载。"
            if stale:
                notice += "本次平台查询成功，但最新可用 K 线仍延迟；自动刷新继续，不补造缺失柱。"
            return (
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bars": bars,
                    "notice": notice,
                    "checked_at": utcnow().isoformat(),
                },
                at,
                notice,
                stale,
            )

        return self.cache.get(
            f"market-{symbol}-{timeframe}", fetch, source="Alpaca Crypto US", ttl=8, refresh=refresh
        )
