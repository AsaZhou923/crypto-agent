import { useEffect, useMemo, useState, type ReactNode } from "react";
import { EquityChart, MarketChart } from "./Charts";
import {
  dateTime,
  activityTime,
  money,
  pct,
  quantity,
  ratingLabels,
  statusLabel,
} from "./format";
import { useMonitor } from "./useMonitor";
import { SchedulerControl } from "./SchedulerControl";
import { marketPrice } from "./market";
import type { Dashboard, Decision, Metric, Section } from "./types";
const EMPTY: never[] = [];
function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    activity: <path d="M2 13h4l4-9 4 16 4-9h4" />,
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1" />
      </>
    ),
    shield: (
      <>
        <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z" />
        <path d="m8 12 3 3 5-6" />
      </>
    ),
    arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>
    ),
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    database: (
      <>
        <ellipse cx="12" cy="5" rx="8" ry="3" />
        <path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.activity}
    </svg>
  );
}
function Status({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
function Empty({ children = "暂无记录" }: { children?: ReactNode }) {
  return (
    <div className="empty">
      <Icon name="database" size={27} />
      <span>{children}</span>
    </div>
  );
}
function Source({
  section,
  compact = false,
  staleLabel = "数据已过期",
}: {
  staleLabel?: string;
  section?: Section<unknown> | null;
  compact?: boolean;
}) {
  if (!section) return <span className="source">正在读取…</span>;
  return (
    <div className="source">
      <span
        className={`dot ${section.error || section.stale ? "amber" : section.data ? "green" : ""}`}
      />
      {section.source}
      {section.stale && <span className="warn">{staleLabel}</span>}
      {!compact && <span>更新于 {dateTime(section.as_of, true)} JST</span>}
    </div>
  );
}
function MetricCard({
  label,
  metric,
  value,
  sub,
  hero = false,
}: {
  label: string;
  metric?: Metric;
  value?: string;
  sub?: string;
  hero?: boolean;
}) {
  const unknown = metric && metric.value == null && metric.percent == null;
  return (
    <article className={`metric ${hero ? "hero-metric" : ""}`}>
      <div className="metric-label">
        {label}
        <span className="metric-hint" title={metric?.basis || sub}>
          ⓘ
        </span>
      </div>
      <div
        className={`metric-value ${unknown ? "unavailable" : metric && Number(metric.value ?? metric.percent) > 0 ? "positive" : metric && Number(metric.value ?? metric.percent) < 0 ? "negative" : ""}`}
      >
        {unknown
          ? "暂无足够数据"
          : (value ??
            (metric?.value != null
              ? money(metric.value, true)
              : pct(metric?.percent, true)))}
      </div>
      <div className="metric-sub">
        {metric?.percent != null && metric.value != null && (
          <strong
            className={Number(metric.percent) >= 0 ? "positive" : "negative"}
          >
            {pct(metric.percent, true)}{" "}
          </strong>
        )}
        {sub || metric?.subtitle || (unknown ? "等待完整历史与资金流记录" : "已校正已知入出金")}
        {metric?.stale && <span className="warn"> · 统计数据已过期</span>}
      </div>
      {metric && (
        <details className="basis">
          <summary>统计口径</summary>
          <p>{metric.basis}</p>
        </details>
      )}
    </article>
  );
}
function DecisionPanel({
  decision,
  onTrace,
  clock,
}: {
  decision: Decision | undefined;
  onTrace: (d: Decision) => void;
  clock: string;
}) {
  if (!decision) return <Empty>数据库中没有决策记录</Empty>;
  const latestRisk = decision.risk.at(-1);
  const expired = Date.parse(decision.expires_at) < Date.parse(clock);
  return (
    <div className="decision-body">
      <div className="decision-top">
        <span className="symbol">{decision.symbol}</span>
        <Status
          tone={
            decision.rating === "REVIEW"
              ? "amber"
              : ["Buy", "Overweight"].includes(decision.rating)
                ? "green"
                : decision.rating === "Hold"
                  ? "purple"
                  : "red"
          }
        >
          {ratingLabels[decision.rating] || decision.rating}
          <span className="rating-en">{decision.rating}</span>
        </Status>
      </div>
      <div className="decision-time">{dateTime(decision.created_at)} JST</div>
      <div className="model-line">
        <span>{decision.strategy_version}</span>
        <span>{decision.model || "模型未记录"}</span>
      </div>
      <div className="position-flow">
        <div>
          <label>决策时仓位</label>
          <strong>{pct(decision.current_position_pct)}</strong>
        </div>
        <Icon name="arrow" />
        <div>
          <label>目标仓位</label>
          <strong>{pct(decision.target_position_pct)}</strong>
        </div>
      </div>
      <div className="reason">
        <h3>决策依据</h3>
        <p>{decision.reason || "未记录"}</p>
        {decision.evidence.length > 0 && (
          <ul>
            {decision.evidence.slice(0, 3).map((x, i) => (
              <li key={i}>{x}</li>
            ))}
          </ul>
        )}
      </div>
      <div
        className={`risk-box ${latestRisk ? (latestRisk.allowed ? "passed" : "blocked") : ""}`}
      >
        <div className="risk-title">
          <Icon name="shield" />
          <strong>独立风控</strong>
          <span>
            {latestRisk ? (latestRisk.allowed ? "通过" : "拒绝") : "未记录"}
          </span>
        </div>
        {latestRisk ? (
          <>
            <p>
              {latestRisk.reasons.length
                ? latestRisk.reasons.join("；")
                : "已保存通过结果；没有额外原因记录。"}
            </p>
            <span className="micro">
              阶段：{latestRisk.phase} · {dateTime(latestRisk.created_at, true)}{" "}
              JST
            </span>
          </>
        ) : (
          <p>尚无可展示的风控记录</p>
        )}
      </div>
      <div className="expiry">
        <Icon name="clock" />
        <span>有效期至 {dateTime(decision.expires_at, true)} JST</span>
        <span className={expired ? "warn" : "positive"}>
          {expired ? "已到期" : "有效"}
        </span>
      </div>
      <div className="decision-actions">
        <details>
          <summary>查看已保存原始报告</summary>
          <pre>{JSON.stringify(decision.raw, null, 2)}</pre>
          <p className="micro">
            仅展示数据库保存的决策字段；完整上游报告未保存时无法恢复。
          </p>
        </details>
        <button className="text-button" onClick={() => onTrace(decision)}>
          追踪订单与成交 <Icon name="arrow" size={14} />
        </button>
      </div>
    </div>
  );
}
type Tab = "positions" | "orders" | "fills" | "decisions" | "logs";
const tabs: [Tab, string][] = [
  ["positions", "持仓"],
  ["orders", "订单"],
  ["fills", "成交"],
  ["decisions", "决策历史"],
  ["logs", "运行日志"],
];
type Row = {
  id: string;
  search: string;
  status: string;
  sort: string | number;
  cells: ReactNode[];
};
function Details({
  dashboard,
  trace,
  clearTrace,
  onTrace,
  tab,
  setTab,
}: {
  dashboard: Dashboard | null;
  trace: Decision | null;
  clearTrace: () => void;
  onTrace: (d: Decision) => void;
  tab: Tab;
  setTab: (t: Tab) => void;
}) {
  const [filter, setFilter] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState("desc");
  const [page, setPage] = useState(1);
  useEffect(() => {
    setFilter("");
    setStatus("");
    setPage(1);
  }, [tab, trace?.run_id]);
  const data = dashboard?.sections;
  const labels: Record<Tab, string[]> = {
    positions: [
      "交易对",
      "持有数量",
      "平均成本",
      "当前价格",
      "市值",
      "未实现盈亏",
    ],
    orders: [
      "交易对 / 订单",
      "方向",
      "订单数量",
      "状态",
      "已成交数量",
      "成交均价",
      "提交时间 · JST",
      "关联决策",
    ],
    fills: [
      "交易对 / 成交",
      "方向",
      "成交数量",
      "成交价格",
      "费用 · USD",
      "成交时间 · JST",
      "关联订单",
    ],
    decisions: ["时间 · JST", "交易对", "评级", "决策依据", "风控", "关联订单"],
    logs: ["开始时间 · JST", "阶段", "状态", "耗时", "错误 / 告警", "关联运行"],
  };
  const rows = useMemo((): Row[] => {
    if (!data) return [];
    if (tab === "positions")
      return (data.account.data?.positions || []).map((p) => ({
        id: p.symbol,
        search: p.symbol,
        status: "",
        sort: Number(p.market_value || 0),
        cells: [
          <strong>{p.symbol}</strong>,
          quantity(p.quantity),
          money(p.average_entry_price),
          money(p.current_price),
          money(p.market_value),
          <span
            className={Number(p.unrealized_pnl) >= 0 ? "positive" : "negative"}
          >
            {money(p.unrealized_pnl, true)}
          </span>,
        ],
      }));
    if (tab === "orders")
      return (data.ledger.data?.orders || [])
        .filter(
          (o) =>
            !trace ||
            o.run_id === trace.run_id ||
            trace.order_ids.includes(o.id),
        )
        .map((o) => ({
          id: o.id,
          search: `${o.symbol} ${o.id} ${o.client_order_id}`,
          status: o.status,
          sort: o.submitted_at || "",
          cells: [
            <>
              <strong>{o.symbol}</strong>
              <small title={o.id}>{o.id.slice(0, 18)}</small>
            </>,
            <span className={o.side === "buy" ? "positive" : "negative"}>
              {o.side === "buy" ? "↗ 买入" : "↘ 卖出"}
            </span>,
            quantity(o.quantity),
            <Status
              tone={
                o.status === "filled"
                  ? "green"
                  : o.status === "partially_filled"
                    ? "amber"
                    : ""
              }
            >
              {statusLabel(o.status)}
            </Status>,
            <>
              {quantity(o.filled_quantity)}
              <div className="fill-meter">
                <i
                  style={{
                    width: `${Math.min(100, (Number(o.filled_quantity) / Number(o.quantity)) * 100)}%`,
                  }}
                />
              </div>
            </>,
            money(o.filled_avg_price),
            dateTime(o.submitted_at, true),
            o.run_id ? (
              <button
                className="text-button"
                onClick={() => {
                  const d = data.agent.data?.decisions.find(
                    (d) => d.run_id === o.run_id,
                  );
                  if (d) onTrace(d);
                }}
                title={o.run_id}
              >
                {o.run_id.slice(0, 12)}
              </button>
            ) : (
              <span
                className="muted"
                title="平台订单未匹配当前数据库中已保存的客户端订单 ID"
              >
                无法关联
              </span>
            ),
          ],
        }));
    if (tab === "fills")
      return (data.ledger.data?.fills || [])
        .filter(
          (f) =>
            !trace ||
            f.run_id === trace.run_id ||
            trace.order_ids.includes(f.order_id),
        )
        .map((f) => ({
          id: f.id,
          search: `${f.symbol} ${f.order_id} ${f.id}`,
          status: f.side,
          sort: f.occurred_at,
          cells: [
            <>
              <strong>{f.symbol}</strong>
              <small title={f.id}>{f.id.slice(0, 18)}</small>
            </>,
            <span className={f.side === "buy" ? "positive" : "negative"}>
              {f.side === "buy" ? "↗ 买入" : "↘ 卖出"}
            </span>,
            quantity(f.quantity),
            money(f.price),
            f.fee == null ? (
              <span className="muted">未归属 / 待入账</span>
            ) : (
              money(f.fee)
            ),
            dateTime(f.occurred_at, true),
            <span title={f.order_id}>{f.order_id.slice(0, 18)}</span>,
          ],
        }));
    if (tab === "decisions")
      return (data.agent.data?.decisions || []).map((d) => ({
        id: d.run_id,
        search: `${d.symbol} ${d.reason} ${d.rating} ${d.run_id}`,
        status: d.rating,
        sort: d.created_at,
        cells: [
          dateTime(d.created_at, true),
          <strong>{d.symbol}</strong>,
          <Status tone={d.rating === "REVIEW" ? "amber" : "purple"}>
            {ratingLabels[d.rating] || d.rating}
          </Status>,
          <span className="reason-cell" title={d.reason}>
            {d.reason}
          </span>,
          d.risk.length ? (
            <span
              className={d.risk.at(-1)!.allowed ? "positive" : "negative"}
              title={d.risk.at(-1)!.reasons.join("；")}
            >
              {d.risk.at(-1)!.allowed ? "✓ 通过" : "× 拒绝"}
            </span>
          ) : (
            "未记录"
          ),
          <button className="text-button" onClick={() => onTrace(d)}>
            追踪 {d.order_ids.length} 笔 <Icon name="arrow" size={14} />
          </button>,
        ],
      }));
    return (data.agent.data?.logs || []).map((l) => ({
      id: l.id,
      search: `${l.phase} ${l.error || ""} ${l.run_id || ""}`,
      status: l.status,
      sort: l.started_at,
      cells: [
        dateTime(l.started_at, true),
        l.phase,
        <Status tone={l.error ? "red" : ""}>{statusLabel(l.status)}</Status>,
        l.duration_ms == null
          ? "未记录"
          : `${(l.duration_ms / 1000).toFixed(2)} s`,
        <span className="reason-cell" title={l.error || ""}>
          {l.error || "—"}
        </span>,
        <span title={l.run_id || ""}>
          {l.run_id?.slice(0, 16) || "未关联"}
        </span>,
      ],
    }));
  }, [data, tab, trace, onTrace]);
  const filtered = rows
    .filter(
      (r) =>
        r.search.toLowerCase().includes(filter.toLowerCase()) &&
        (!status || r.status === status),
    )
    .sort(
      (a, b) =>
        (typeof a.sort === "number" && typeof b.sort === "number"
          ? a.sort - b.sort
          : String(a.sort).localeCompare(String(b.sort))) *
        (sort === "asc" ? 1 : -1),
    );
  const pages = Math.max(1, Math.ceil(filtered.length / 6));
  const currentPage = Math.min(page, pages);
  const section =
    tab === "positions"
      ? data?.account
      : ["orders", "fills"].includes(tab)
        ? data?.ledger
        : data?.agent;
  return (
    <section className="panel detail-panel" id="details">
      <div className="tabs" role="tablist" aria-label="交易记录">
        {tabs.map(([id, label], index) => (
          <button
            key={id}
            role="tab"
            id={`tab-${id}`}
            aria-selected={tab === id}
            aria-controls="detail-content"
            tabIndex={tab === id ? 0 : -1}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
                e.preventDefault();
                const next =
                  tabs[(index + (e.key === "ArrowRight" ? 1 : 4)) % 5][0];
                setTab(next);
                setPage(1);
                setStatus("");
                document.getElementById(`tab-${next}`)?.focus();
              }
            }}
            onClick={() => {
              setTab(id);
              setPage(1);
              setStatus("");
              setFilter("");
            }}
          >
            {label}
            <span>
              {id === "positions"
                ? data?.account.data?.positions.length || 0
                : id === "orders"
                  ? data?.ledger.data?.orders.length || 0
                  : id === "fills"
                    ? data?.ledger.data?.fills.length || 0
                    : id === "decisions"
                      ? data?.agent.data?.decisions.length || 0
                      : data?.agent.data?.logs.length || 0}
            </span>
          </button>
        ))}
      </div>
      {trace && (
        <div className="trace-banner">
          <span>
            决策追踪 <b>{trace.run_id}</b> ·{" "}
            {ratingLabels[trace.rating] || trace.rating} · {trace.reason}
          </span>
          <button onClick={clearTrace}>清除追踪 ×</button>
        </div>
      )}
      <>
        {trace && (
          <details className="trace-report">
            <summary>查看此决策的目标仓位、有效期、全部风控与原始记录</summary>
            <p>
              {trace.strategy_version} · {trace.model} ·{" "}
              {dateTime(trace.created_at)} JST
            </p>
            <p>
              决策时仓位 {pct(trace.current_position_pct)} → 目标{" "}
              {pct(trace.target_position_pct)} · 有效期至{" "}
              {dateTime(trace.expires_at)} JST
            </p>
            {trace.risk.map((r, i) => (
              <p key={i}>
                {r.phase} · {r.allowed ? "通过" : "拒绝"} ·{" "}
                {r.reasons.join("；") || "未记录额外原因"}
              </p>
            ))}
            <pre>{JSON.stringify(trace.raw, null, 2)}</pre>
          </details>
        )}
      </>
      <div className="table-tools">
        <Source section={section} />
        <div className="filters">
          <input
            aria-label="搜索记录"
            placeholder="搜索交易对、ID 或理由…"
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              setPage(1);
            }}
          />
          {tab !== "positions" && (
            <select
              aria-label="筛选状态"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(1);
              }}
            >
              <option value="">全部状态</option>
              {[...new Set(rows.map((r) => r.status))].map((s) => (
                <option key={s} value={s}>
                  {statusLabel(ratingLabels[s] || s)}
                </option>
              ))}
            </select>
          )}
          <select
            aria-label="排序"
            value={sort}
            onChange={(e) => setSort(e.target.value)}
          >
            <option value="desc">
              {tab === "positions" ? "市值从高到低" : "最新在前"}
            </option>
            <option value="asc">
              {tab === "positions" ? "市值从低到高" : "最早在前"}
            </option>
          </select>
        </div>
      </div>
      <div
        id="detail-content"
        role="tabpanel"
        aria-labelledby={`tab-${tab}`}
        tabIndex={0}
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {labels[tab].map((l) => (
                  <th key={l}>{l}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered
                .slice((currentPage - 1) * 6, currentPage * 6)
                .map((r) => (
                  <tr key={r.id}>
                    {r.cells.map((c, i) => (
                      <td key={i}>{c}</td>
                    ))}
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
        {!filtered.length && (
          <Empty>
            {section?.error
              ? section.error
              : filter || status
                ? "没有匹配的记录，请调整筛选条件"
                : trace
                  ? "没有已关联记录；数据库未保存订单关联或该决策没有提交订单。"
                  : "当前数据范围内暂无记录"}
          </Empty>
        )}
      </div>
      {tab === "fills" && !!data?.ledger.data?.fees.length && (
        <details className="fee-details">
          <summary>
            独立费用记录 · {data.ledger.data.fees.length}{" "}
            条（无订单归属时不摊派到成交）
          </summary>
          {data.ledger.data.fees.map((f) => (
            <p key={f.id}>
              {activityTime(f.occurred_at)} · {f.symbol || "交易对未提供"} ·{" "}
              {f.attribution === "asset fee attributed to symbol"
                ? "扣币手续费"
                : f.currency === "USD"
                  ? "USD 现金手续费"
                  : f.currency
                    ? `${f.currency} 手续费`
                    : "费用币种未提供"} · 折合{" "}
              {money(f.amount)} ·{" "}
              {f.order_id || "平台未提供关联订单"}
            </p>
          ))}
        </details>
      )}
      <div className="table-footer">
        <span>{filtered.length} 条记录 · 每页 6 条</span>
        <div>
          <button
            aria-label="上一页"
            disabled={currentPage === 1}
            onClick={() => setPage(currentPage - 1)}
          >
            ‹
          </button>
          <span>
            {currentPage} / {pages}
          </span>
          <button
            aria-label="下一页"
            disabled={currentPage === pages}
            onClick={() => setPage(currentPage + 1)}
          >
            ›
          </button>
        </div>
      </div>
      <p className="table-notice">
        {tab === "positions"
          ? "当前价格与未实现盈亏来自所标数据源；演示模式为固定合成账户。"
          : ["orders", "fills"].includes(tab)
            ? data?.ledger.data?.notice || section?.notice
            : data?.agent.data?.notice || section?.notice}
      </p>
    </section>
  );
}
export default function App() {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);
  const [symbol, setSymbol] = useState("BTC/USD");
  const [timeframe, setTimeframe] = useState("5Min");
  const [tab, setTab] = useState<Tab>("positions");
  const [trace, setTrace] = useState<Decision | null>(null);
  const {
    dashboard: receivedDashboard,
    market: receivedMarket,
    busy,
    marketBusy,
    error,
    refresh,
  } = useMonitor(timeframe, symbol);
  const dashboard = useMemo(() => {
    if (!receivedDashboard || receivedDashboard.mode === "demo")
      return receivedDashboard;
    const limits: Record<string, number> = {
      account: 90,
      ledger: 90,
      agent: 1800,
      equity: 120,
    };
    const sections = Object.fromEntries(
      Object.entries(receivedDashboard.sections).map(([key, section]) => [
        key,
        section.as_of &&
        now - Date.parse(section.as_of) > limits[key] * 1000 &&
        !section.stale
          ? { ...section, stale: true }
          : section,
      ]),
    ) as Dashboard["sections"];
    return { ...receivedDashboard, sections };
  }, [receivedDashboard, now]);
  const market = useMemo(() => {
    if (receivedMarket?.data && (receivedMarket.data.symbol !== symbol || receivedMarket.data.timeframe !== timeframe)) return null;
    if (
      !receivedMarket ||
      receivedDashboard?.mode === "demo" ||
      !receivedMarket.as_of
    )
      return receivedMarket;
    const interval =
      timeframe === "1Hour" ? 3600 : timeframe === "5Min" ? 300 : 60;
    return now - Date.parse(receivedMarket.as_of) > (interval + 120) * 1000
      ? { ...receivedMarket, stale: true }
      : receivedMarket;
  }, [receivedMarket, receivedDashboard?.mode, now, timeframe, symbol]);
  const sections = dashboard?.sections;
  const account = sections?.account.data;
  const agent = sections?.agent.data;
  const latest = agent?.decisions[0];
  const isDemo = dashboard?.mode === "demo";
  const connection =
    error || sections?.account.error
      ? "连接异常"
      : sections?.account.stale
        ? "数据已过期"
        : account
          ? isDemo
            ? "离线演示"
            : "平台已连接"
          : "等待连接";
  const stale = sections && Object.values(sections).some((s) => s.stale);
  const failures = sections
    ? Object.entries(sections).filter(([, s]) => s.error)
    : [];
  const traceDecision = (d: Decision) => {
    setTrace(d);
    setTab("orders");
    document
      .getElementById("details")
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  return (
    <>
      <a href="#main" className="skip">
        跳转到主要内容
      </a>
      <header className="app-header">
        <div className="brand">
          <span className="brand-icon">
            <Icon name="activity" size={24} />
          </span>
          <div>
            Crypto Agent<small>TRADING OBSERVATORY</small>
          </div>
          <Status tone="green">PAPER</Status>
        </div>
        <div className="header-right">
          <span className="readonly">
            <Icon name="shield" size={15} /> 本地 Paper
          </span>
          <span className="timezone">
            Asia/Tokyo <b>UTC+9</b>
          </span>
        </div>
      </header>
      <nav className="top-nav" aria-label="页面导航">
        <a href="#main" className="active">
          <Icon name="grid" size={15} />
          监控总览
        </a>
        <a href="#details">
          <Icon name="database" size={15} />
          交易记录
        </a>
        <span>
          LOCAL WORKSPACE <span className="dot green" />
        </span>
      </nav>
      <main id="main">
        <div className="page-heading">
          <div>
            <div className="eyebrow">WORKSPACE / OVERVIEW</div>
            <h1>
              交易监控台<span>账户、决策与执行，全程可追踪。</span>
            </h1>
          </div>
          <div className="refresh-group">
            <div>
              <span
                className={`dot ${error || sections?.account.error || sections?.account.stale ? "amber" : account ? "green" : ""}`}
              />
              {connection}
              <small>
                {dashboard
                  ? `${dateTime(dashboard.as_of, true)} JST${isDemo ? " · 固定演示时钟" : " · 最近请求"}`
                  : "等待首次同步"}
              </small>
              <small className="auto-refresh-status">
                自动刷新 · 账户/行情约10秒 · Agent 5秒 · 收益60秒
              </small>
            </div>
            <button
              className="refresh-button"
              disabled={busy || marketBusy}
              onClick={refresh}
            >
              <span className={busy || marketBusy ? "spin" : ""}>
                <Icon name="refresh" size={16} />
              </span>
              {busy || marketBusy ? "同步中" : "刷新数据"}
            </button>
          </div>
        </div>
        <SchedulerControl />
        {isDemo && (
          <div className="demo-banner">
            <Status tone="amber">演示数据</Status>
            <span>离线演示 · 固定样本，非实时账户与行情</span>
            <span className="banner-right">2026.09.19 · 可重复验证</span>
          </div>
        )}
        {(error || failures.length > 0 || stale) && (
          <div className="alert" role="status">
            <b>
              {error
                ? "连接中断"
                : failures.length
                  ? "部分数据不可用"
                  : "部分数据更新延迟"}
            </b>
            <span>
              {error ||
                failures
                  .map(
                    ([k, s]) =>
                      `${({ account: "账户", ledger: "订单/成交", agent: "Agent", equity: "权益" } as Record<string, string>)[k]}：${s.error}`,
                  )
                  .join("；") ||
                Object.entries(sections || {})
                  .filter(([, s]) => s.stale)
                  .map(
                    ([k, s]) =>
                      `${({ account: "账户", ledger: "订单/成交", agent: "Agent 运行记录", equity: "权益曲线" } as Record<string, string>)[k]}：最后观察 ${dateTime(s.as_of, true)} JST${k === "agent" ? "，尚无新的运行记录" : "，正在自动重试"}`,
                  )
                  .join("；")}{" "}
            </span>
          </div>
        )}
        <div className="section-label">
          <span>
            账户概览 <span className="muted">/ USD</span>
          </span>
          <Source section={sections?.account} />
        </div>
        <section
          className={`metrics ${!dashboard ? "loading" : ""}`}
          aria-label="账户概览"
          aria-busy={!dashboard}
        >
          <MetricCard
            label="账户总资产"
            value={money(account?.equity)}
            sub="当前账户权益 · 含现金与持仓"
            hero
          />
          <MetricCard
            label="可用现金"
            value={money(account?.buying_power)}
            sub={`现金余额 ${money(account?.cash)} · 可用取现金与非保证金购买力较小值`}
          />
          <MetricCard
            label="今日盈亏"
            metric={
              account?.metrics.daily || {
                value: null,
                percent: null,
                basis: "Asia/Tokyo 自然日；需要日初权益及完整资金流。",
              }
            }
          />
          <MetricCard
            label="累计收益"
            metric={
              account?.metrics.total || {
                value: null,
                percent: null,
                basis: "完整观察区间内校正入出金后的收益。",
              }
            }
          />
          <MetricCard
            label="最大回撤"
            metric={
              account?.metrics.drawdown || {
                value: null,
                percent: null,
                basis: "完整观察区间、经资金流校正的净值峰谷回撤。",
              }
            }
          />
        </section>
        <div className="primary-grid">
          <div className="chart-column">
            <section className="panel market-panel" aria-label="交易标的行情">
              <div className="instrument-tabs" role="group" aria-label="行情标的">
                {["BTC/USD", "XRP/USD"].map((pair) => (
                  <button key={pair} aria-pressed={symbol === pair} onClick={() => setSymbol(pair)}>
                    {pair}
                  </button>
                ))}
                <span>现货行情</span>
              </div>
              <div className="panel-heading">
                <div className="instrument">
                  <span className={`coin ${symbol === "XRP/USD" ? "coin-xrp" : ""}`} aria-hidden="true">{symbol === "BTC/USD" ? "₿" : "X"}</span>
                  <div>
                    <h2>
                      {symbol.split("/")[0]} <span>/ USD</span>
                    </h2>
                    <small>{symbol === "BTC/USD" ? "Bitcoin" : "XRP Ledger"} · 现货行情</small>
                  </div>
                  {market?.data?.bars.length ? (
                    <strong className="price">
                      {marketPrice(market.data.bars.at(-1)!.close, symbol)}
                      <small>末根 K 线收盘价</small>
                    </strong>
                  ) : null}
                </div>
                <div className="periods" aria-label="K线周期">
                  {[
                    ["1Min", "1 分"],
                    ["5Min", "5 分"],
                    ["1Hour", "1 小时"],
                  ].map(([id, label]) => (
                    <button
                      key={id}
                      aria-pressed={timeframe === id}
                      onClick={() => setTimeframe(id)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="chart-meta">
                <Source section={market} staleLabel={market?.error ? "行情更新失败" : "平台 K 线延迟"} />
                <span className="micro">{market?.data?.checked_at ? `查询于 ${dateTime(market.data.checked_at, true)} JST` : "USD · Asia/Tokyo"}</span>
              </div>
              {market?.error && (
                <p className="inline-warning" role="status">
                  {market.error}
                </p>
              )}
              {market?.data?.bars.length ? (
                <MarketChart
                  data={market.data}
                  fills={sections?.ledger.data?.fills || EMPTY}
                  decisions={agent?.decisions || EMPTY}
                />
              ) : (
                <div className={marketBusy ? "chart-skeleton" : ""}>
                  <Empty>
                    {marketBusy ? "正在读取 K 线与成交量…" : "暂无可用行情"}
                  </Empty>
                </div>
              )}
              <div className="chart-legend">
                <span className="positive">▲ 买入成交</span>
                <span className="negative">▼ 卖出成交</span>
                <span className="lavender">● Agent 信号</span>
                <span className="muted">▥ 成交量</span>
              </div>
              <p className="chart-note">
                {market?.data?.notice ||
                  "成交箭头对应平台逐笔成交；信号不代表已下单。"}{" "}
                仅标注 {symbol} 在当前图表时间范围内的事件。
              </p>
            </section>
            <section className="panel equity-panel">
              <div className="panel-heading">
                <div>
                  <h2>
                    账户权益曲线 <span className="unit">USD</span>
                  </h2>
                  <p className="micro">独立账户序列 · 权益余额不等于收益</p>
                </div>
                <Source section={sections?.equity} />
              </div>
              {sections?.equity.data &&
              sections.equity.data.points.length >= 2 ? (
                <EquityChart data={sections.equity.data} />
              ) : (
                <Empty>暂无足够权益历史数据</Empty>
              )}
              <p className="chart-note">
                {sections?.equity.data?.basis ||
                  "需要至少两次账户权益观察；不使用 BTC 价格替代。"}
              </p>
            </section>
          </div>
          <section className="panel agent-panel">
            <div className="panel-heading">
              <div>
                <span className="eyebrow">AGENT INTELLIGENCE</span>
                <h2>最新 Agent 决策</h2>
              </div>
              <span className="agent-icon">
                <Icon name="activity" />
              </span>
            </div>
            <div className="agent-status">
              <Source section={sections?.agent} compact />
              <span>{agent ? statusLabel(agent.status) : "读取中"}</span>
            </div>
            <DecisionPanel
              decision={latest}
              onTrace={traceDecision}
              clock={
                isDemo && dashboard
                  ? dashboard.as_of
                  : new Date(now).toISOString()
              }
            />
            <div className="agent-bottom">
              <span>最近运行</span>
              <span>{dateTime(agent?.last_run_at, true)} JST</span>
            </div>
          </section>
        </div>
        <Details
          dashboard={dashboard}
          trace={trace}
          clearTrace={() => setTrace(null)}
          onTrace={traceDecision}
          tab={tab}
          setTab={setTab}
        />
        <footer>
          <span>
            <span className="dot green" /> Crypto Agent{" "}
            <span className="muted">/</span> 本地监控 · Paper only
          </span>
          <span>
            图表：
            <a
              href="https://www.tradingview.com/"
              target="_blank"
              rel="noreferrer"
            >
              TradingView Lightweight Charts™
            </a>{" "}
            ·{" "}
            <a href="/NOTICE.txt" target="_blank" rel="noreferrer">
              许可与署名
            </a>
          </span>
        </footer>
      </main>
    </>
  );
}
