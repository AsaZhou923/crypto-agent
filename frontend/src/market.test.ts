import { test } from "node:test";
import assert from "node:assert/strict";
import { marketMarkers, marketPrice, marketPrecision } from "./market.ts";
import type { Decision, Fill, Market } from "./types.ts";

const time = "2026-09-21T08:00:00Z";
const data: Market = {
  symbol: "XRP/USD", timeframe: "1Min", notice: "test",
  bars: [{ time, open: "1.4421", high: "1.4428", low: "1.4420", close: "1.4426", volume: "2000" }],
};
const fill = (symbol: string, side: string, occurred_at = time) => ({ symbol, side, occurred_at }) as Fill;
const decision = (symbol: string, created_at = time) => ({ symbol, created_at }) as Decision;

test("selected pair isolates fills and signals, accepting saved compact symbols", () => {
  const fills = [fill("BTC/USD", "buy"), fill("XRP/USD", "buy"), fill("XRPUSD", "sell")];
  const decisions = [decision("BTCUSD"), decision("XRPUSD")];
  const xrp = marketMarkers(data, fills, decisions);
  assert.deepEqual(xrp.map((marker) => marker.shape), ["arrowUp", "arrowDown", "circle"]);
  assert.equal(xrp[0].position, "belowBar");
  assert.equal(xrp[1].position, "aboveBar");
  const btc = marketMarkers({ ...data, symbol: "BTC/USD" }, fills, decisions);
  assert.deepEqual(btc.map((marker) => marker.shape), ["arrowUp", "circle"]);
});

test("markers only appear within the selected candle period", () => {
  const markers = marketMarkers(data, [
    fill("XRP/USD", "buy", "2026-09-21T07:59:59Z"),
    fill("XRP/USD", "buy", "2026-09-21T08:00:59Z"),
    fill("XRP/USD", "sell", "2026-09-21T08:01:00Z"),
  ], []);
  assert.equal(markers.length, 1);
  assert.equal(markers[0].time, Date.parse(time) / 1000);
});

test("XRP price retains sub-cent movements while BTC uses cent precision", () => {
  assert.equal(marketPrecision("XRP/USD"), 4);
  assert.equal(marketPrice("1.4426", "XRP/USD"), "$1.4426");
  assert.equal(marketPrice("81900.1234", "BTC/USD"), "$81,900.12");
});
