export type Section<T> = {
  source: string;
  as_of: string | null;
  stale: boolean;
  error: string | null;
  data: T | null;
  notice?: string;
};
export type Metric = {
  subtitle?: string;
  as_of?: string;
  source?: string;
  stale?: boolean;
  error?: string | null;
  value: string | null;
  percent: string | null;
  basis: string;
};
export type Position = {
  symbol: string;
  quantity: string;
  average_entry_price: string;
  current_price: string | null;
  market_value: string | null;
  unrealized_pnl: string | null;
};
export type Account = {
  equity: string;
  cash: string;
  buying_power: string;
  positions: Position[];
  metrics: { daily: Metric; total: Metric; drawdown: Metric };
};
export type Order = {
  id: string;
  client_order_id: string;
  symbol: string;
  side: string;
  quantity: string;
  filled_quantity: string;
  filled_avg_price: string | null;
  status: string;
  submitted_at: string | null;
  run_id: string | null;
};
export type Fill = {
  id: string;
  order_id: string;
  symbol: string;
  side: string;
  quantity: string;
  price: string;
  fee: string | null;
  occurred_at: string;
  run_id: string | null;
};
export type Ledger = {
  orders: Order[];
  fills: Fill[];
  fees: {
    id: string;
    order_id: string | null;
    amount: string | null;
    currency?: string | null;
    symbol?: string | null;
    attribution?: string;
    occurred_at: string;
  }[];
  notice: string;
};
export type Decision = {
  run_id: string;
  symbol: string;
  created_at: string;
  expires_at: string;
  rating: string;
  strategy_version: string;
  model: string;
  target_position_pct: string | null;
  current_position_pct: string | null;
  reason: string;
  evidence: string[];
  risk: {
    phase: string;
    allowed: boolean;
    reasons: string[];
    created_at: string;
  }[];
  order_ids: string[];
  raw: unknown;
};
export type Agent = {
  decisions: Decision[];
  logs: {
    id: string;
    run_id: string | null;
    phase: string;
    status: string;
    started_at: string;
    duration_ms: number | null;
    error: string | null;
  }[];
  status: string;
  last_run_at: string | null;
  notice: string;
};
export type Equity = {
  points: { time: string; value: string }[];
  basis: string;
};
export type Dashboard = {
  mode: "demo" | "paper";
  as_of: string;
  timezone: string;
  sections: {
    account: Section<Account>;
    ledger: Section<Ledger>;
    agent: Section<Agent>;
    equity: Section<Equity>;
  };
};
export type Market = {
  checked_at?: string;
  symbol: string;
  timeframe: string;
  bars: {
    time: string;
    open: string;
    high: string;
    low: string;
    close: string;
    volume: string;
  }[];
  notice: string;
};
