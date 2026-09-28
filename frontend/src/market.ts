import type { Decision, Fill, Market } from "./types";

export const marketPrecision = (symbol: string) => symbol === "XRP/USD" ? 4 : 2;
export function marketPrice(value: string | undefined, symbol: string): string {
  if (value == null) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD",
    minimumFractionDigits: marketPrecision(symbol),
    maximumFractionDigits: marketPrecision(symbol),
  }).format(Number(value));
}

// Saved Agent reports may use XRPUSD while broker responses use XRP/USD.
const pairKey = (symbol: string) => symbol.toUpperCase().replace(/[/-]/g, "");
type Marker = {
  time: number;
  position: "belowBar" | "aboveBar";
  color: string;
  shape: "arrowUp" | "arrowDown" | "circle";
};
export function marketMarkers(data: Market, fills: Fill[], decisions: Decision[]): Marker[] {
  const interval = data.timeframe === "1Hour" ? 3600 : data.timeframe === "5Min" ? 300 : 60;
  const bars = data.bars.map((b) => Date.parse(b.time) / 1000);
  const bucket = (iso: string) => {
    const t = Date.parse(iso) / 1000;
    return bars.findLast((b) => b <= t && t < b + interval);
  };
  const markers: Marker[] = [];
  for (const fill of fills.filter((f) => pairKey(f.symbol) === pairKey(data.symbol))) {
    const time = bucket(fill.occurred_at);
    if (time == null) continue;
    markers.push({ time, position: fill.side === "buy" ? "belowBar" : "aboveBar",
      color: fill.side === "buy" ? "#80e7bd" : "#ff9da9",
      shape: fill.side === "buy" ? "arrowUp" : "arrowDown" });
  }
  for (const decision of decisions.filter((d) => pairKey(d.symbol) === pairKey(data.symbol))) {
    const time = bucket(decision.created_at);
    if (time == null) continue;
    markers.push({ time, position: "aboveBar", color: "#a6a0e8", shape: "circle" });
  }
  return markers.sort((a, b) => a.time - b.time);
}
