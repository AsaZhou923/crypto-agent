export const TZ = "Asia/Tokyo";
export function dateTime(
  value: string | null | undefined,
  short = false,
): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "未记录";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: TZ,
    month: "2-digit",
    day: "2-digit",
    ...(short ? {} : { year: "numeric" }),
    hour: "2-digit",
    minute: "2-digit",
    ...(short ? {} : { second: "2-digit" }),
    hourCycle: "h23",
  }).format(new Date(value));
}
export function activityTime(value: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(value)
    ? `${value}（平台日期；未提供时刻/时区）`
    : `${dateTime(value)} JST`;
}
export function money(
  value: string | number | null | undefined,
  signed = false,
): string {
  if (value == null || value === "" || !Number.isFinite(Number(value)))
    return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits:
      Number(value) !== 0 && Math.abs(Number(value)) < 1 ? 5 : 2,
    signDisplay: signed ? "always" : "auto",
  }).format(Number(value));
}
export function quantity(value: string | number | null | undefined): string {
  return value == null
    ? "—"
    : new Intl.NumberFormat("en-US", { maximumFractionDigits: 8 }).format(
        Number(value),
      );
}
export function pct(
  value: string | number | null | undefined,
  signed = false,
): string {
  if (value == null) return "未记录";
  return new Intl.NumberFormat("en-US", {
    style: "percent",
    maximumFractionDigits: Number(value) !== 0 && Math.abs(Number(value)) < 0.0001 ? 6 : 3,
    signDisplay: signed ? "always" : "auto",
  }).format(Number(value));
}
export const statusLabels: Record<string, string> = {
  analyzed: "分析完成",
  no_runs: "暂无运行",
  passed: "已通过",
  blocked: "已阻断",
  filled: "全部成交",
  partially_filled: "部分成交",
  new: "待成交",
  accepted: "已受理",
  pending_new: "提交中",
  canceled: "已撤销",
  pending_cancel: "撤销中",
  expired: "已到期",
  rejected: "已拒绝",
  replaced: "已替换",
  unknown: "状态待核对",
  preview: "仅预览",
  no_order: "无订单",
  completed: "已完成",
  success: "正常",
  started: "运行中",
  failed: "失败",
  error: "错误",
  risk_rejected: "风控拒绝",
  paused: "已暂停",
  enabled: "已启用",
};
export function statusLabel(status: string) {
  return statusLabels[status] || status;
}
export const ratingLabels: Record<string, string> = {
  Buy: "买入",
  Overweight: "增持",
  Sell: "卖出",
  Underweight: "减持",
  Hold: "持有",
  REVIEW: "待复核",
};
