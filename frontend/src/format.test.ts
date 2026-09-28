import { test } from "node:test";
import assert from "node:assert/strict";
import { dateTime, activityTime, money, pct, statusLabel } from "./format.ts";
test("Tokyo date rolls over at UTC 15:00 independent of local timezone", () => {
  assert.match(dateTime("2026-09-19T15:00:00Z"), /2026\/09\/20 00:00:00/);
});
test("missing performance is never formatted as zero", () => {
  assert.equal(money(null), "—");
  assert.equal(pct(null), "未记录");
  assert.equal(money("0"), "$0.00");
});
test("partial and pending cancellations are distinct from fills", () => {
  assert.equal(statusLabel("partially_filled"), "部分成交");
  assert.equal(statusLabel("pending_cancel"), "撤销中");
  assert.equal(statusLabel("future_status"), "future_status");
});

test("date-only fees never invent an exact time or timezone", () => {
  assert.equal(
    activityTime("2026-09-19"),
    "2026-09-19（平台日期；未提供时刻/时区）",
  );
});

test("small nonzero Paper returns remain visible", () => {
  assert.equal(pct("0.0000022", true), "+0.00022%");
  assert.equal(pct("-0.000001"), "-0.0001%");
});
