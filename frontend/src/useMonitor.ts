import { useCallback, useEffect, useRef, useState } from "react";
import type { Dashboard, Market, Section } from "./types";
const scenario =
  new URLSearchParams(location.search).get("scenario") || "normal";
async function read<T>(url: string): Promise<T> {
  const response = await fetch(url, {
    signal: AbortSignal.timeout(90000),
    cache: "no-store",
  });
  if (!response.ok)
    throw new Error(`本地接口返回 HTTP ${response.status}，请检查后端服务`);
  return response.json();
}
export function useMonitor(timeframe: string, symbol: string) {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [market, setMarket] = useState<Section<Market> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [marketBusy, setMarketBusy] = useState(false);
  const dashboardFlight = useRef(false);
  const marketFlight = useRef(false);
  const alive = useRef(true);
  const currentQuery = useRef({ timeframe, symbol });
  currentQuery.current = { timeframe, symbol };
  const refreshDashboard = useCallback(async (force = false) => {
    if (dashboardFlight.current) return;
    dashboardFlight.current = true;
    setBusy(true);
    try {
      const result = await read<Dashboard>(
        `/api/dashboard?refresh=${force}&scenario=${encodeURIComponent(scenario)}`,
      );
      if (alive.current) {
        setDashboard(result);
        setError(null);
      }
    } catch {
      if (alive.current) {
        setError("本地 API 连接失败。已保留最后成功数据，请检查后端服务。");
        setDashboard((old) =>
          old
            ? {
                ...old,
                sections: Object.fromEntries(
                  Object.entries(old.sections).map(([key, section]) => [
                    key,
                    { ...section, stale: true },
                  ]),
                ) as Dashboard["sections"],
              }
            : null,
        );
      }
    } finally {
      dashboardFlight.current = false;
      if (alive.current) setBusy(false);
    }
  }, []);
  const refreshMarket = useCallback(async (force = false) => {
    if (marketFlight.current) return;
    marketFlight.current = true;
    setMarketBusy(true);
    const requested = currentQuery.current;
    const isCurrent = () => requested.timeframe === currentQuery.current.timeframe && requested.symbol === currentQuery.current.symbol;
    try {
      const result = await read<Section<Market>>(
        `/api/market?symbol=${encodeURIComponent(requested.symbol)}&timeframe=${requested.timeframe}&refresh=${force}&scenario=${encodeURIComponent(scenario)}`,
      );
      if (alive.current && isCurrent())
        setMarket(result);
    } catch {
      if (alive.current && isCurrent())
        setMarket((old) => ({
          source: old?.source || "本地后端",
          as_of: old?.as_of || null,
          stale: true,
          data: old?.data?.timeframe === requested.timeframe && old.data.symbol === requested.symbol ? old.data : null,
          error: "行情请求失败，等待重新连接",
        }));
    } finally {
      marketFlight.current = false;
      if (alive.current) setMarketBusy(false);
    }
  }, []);
  useEffect(() => {
    alive.current = true;
    void refreshDashboard();
    const tick = window.setInterval(() => {
      void refreshDashboard();
    }, 5000);
    return () => {
      alive.current = false;
      clearInterval(tick);
    };
  }, [refreshDashboard]);
  useEffect(() => {
    setMarket(null);
    void refreshMarket();
    // A changed selection is retried after the previous request finishes.
    const tick = window.setInterval(() => {
      void refreshMarket();
    }, 10000);
    return () => clearInterval(tick);
  }, [timeframe, symbol, refreshMarket]);
  useEffect(() => {
    const resume = () => {
      if (document.hidden) return;
      void refreshDashboard();
      void refreshMarket();
    };
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("focus", resume);
    window.addEventListener("online", resume);
    return () => {
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("focus", resume);
      window.removeEventListener("online", resume);
    };
  }, [refreshDashboard, refreshMarket]);
  useEffect(() => {
    if (!marketBusy && !market) void refreshMarket();
  }, [marketBusy, market, refreshMarket]);
  return {
    dashboard,
    market,
    busy,
    marketBusy,
    error,
    refresh: () => {
      void refreshDashboard(true);
      void refreshMarket(true);
    },
  };
}
