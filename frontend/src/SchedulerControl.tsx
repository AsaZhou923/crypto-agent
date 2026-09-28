import { useCallback, useEffect, useRef, useState } from "react";
import { dateTime } from "./format";

type SchedulerStatus = {
  available: boolean;
  enabled: boolean;
  loaded: boolean;
  running: boolean;
  can_start: boolean;
  can_stop: boolean;
  state: string;
  reason: string | null;
  interval_seconds: number;
  symbols: string[];
  as_of: string;
  error: string | null;
};
const labels: Record<string, string> = {
  stopped: "已停止", paused: "交易已暂停", waiting: "已启用 · 等待下一轮",
  running: "正在执行", blocked: "启动受阻", unavailable: "控制不可用",
};

export function SchedulerControl() {
  const [status, setStatus] = useState<SchedulerStatus | null>(null);
  const [pending, setPending] = useState<"start" | "stop" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const reading = useRef<AbortController | null>(null);
  const operation = useRef(false);
  const mounted = useRef(false);
  const refresh = useCallback(async () => {
    if (operation.current || reading.current) return;
    const controller = new AbortController();
    reading.current = controller;
    try {
      const response = await fetch("/api/scheduler", {
        cache: "no-store",
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
      });
      if (!response.ok) throw new Error("状态读取失败");
      const next: SchedulerStatus = await response.json();
      if (mounted.current && reading.current === controller) {
        setStatus(next);
        setStale(false);
      }
    } catch {
      if (mounted.current && !controller.signal.aborted) setStale(true);
    } finally {
      if (reading.current === controller) reading.current = null;
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    const tick = window.setInterval(() => void refresh(), 5000);
    const resume = () => { if (!document.hidden) void refresh(); };
    window.addEventListener("focus", resume);
    window.addEventListener("online", resume);
    document.addEventListener("visibilitychange", resume);
    return () => {
      mounted.current = false;
      clearInterval(tick);
      reading.current?.abort();
      reading.current = null;
      window.removeEventListener("focus", resume);
      window.removeEventListener("online", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [refresh]);

  const change = async (action: "start" | "stop") => {
    if (operation.current) return;
    operation.current = true;
    reading.current?.abort();
    reading.current = null;
    setPending(action);
    setNotice(null);
    setError(null);
    try {
      const response = await fetch("/api/scheduler", {
        method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-Crypto-Agent-Control": "1" },
        body: JSON.stringify({ action }), signal: AbortSignal.timeout(60000),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "操作失败，请核对当前状态。");
      if (mounted.current) {
        setStatus(result);
        setStale(false);
        setNotice(action === "start" ? "已启用；等待下一次定时触发。" : "已停止新提交和调度。已受理订单仍需核对。");
      }
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error && failure.name === "Error"
        ? failure.message : "操作结果未确认，正在重新读取状态；不会自动重试启停。");
    } finally {
      operation.current = false;
      if (mounted.current) {
        setPending(null);
        void refresh();
      }
    }
  };
  const on = !!status?.enabled;
  const action = on ? "stop" : "start";
  // A stale page may still offer an explicitly permitted stop, but never a start.
  const allowed = status && (action === "stop" ? status.can_stop : status.can_start && !stale);
  return (
    <section className={`scheduler-control ${on ? "scheduler-on" : ""}`} aria-label="自动交易控制" aria-busy={pending !== null}>
      <div className="scheduler-main">
        <div className="scheduler-heading">
          <span className={`dot ${on ? "green" : "amber"}`} />
          <h2>自动交易</h2>
          <span className="scheduler-source">自动调度 · Paper</span>
        </div>
        <p className="scheduler-state">
          {stale ? "状态连接中断 · 保留上次结果" : status ? labels[status.state] || status.state : "正在读取调度状态…"}
          {status?.available && <span> · {status.loaded ? "调度已加载" : "调度未加载"} · {status.symbols.join("、")} · 每 {status.interval_seconds / 60} 分钟</span>}
        </p>
        <p className="scheduler-note">开启后按现有策略自动模拟下单；停止不会撤销已受理订单。</p>
        {(status?.reason || status?.error) && <p className="scheduler-reason">{status.reason || status.error}</p>}
        <div className="scheduler-feedback" role="status" aria-live="polite">
          {error ? <span className="negative">{error}</span> : notice}
        </div>
      </div>
      <div className="scheduler-actions">
        <button className="scheduler-switch" role="switch" aria-checked={on}
          aria-label="自动交易启停" disabled={!allowed || pending !== null}
          onClick={() => void change(action)}>
          <span className="switch-track" aria-hidden="true"><span /></span>
          <span>{pending ? pending === "start" ? "正在启动…" : "正在停止…" : on ? "停止自动交易" : "启动自动交易"}</span>
        </button>
        {status?.available && !on && (status.loaded || status.running || status.error || stale) && (
          <button className="scheduler-retry" onClick={() => void change("stop")} disabled={pending !== null}>停止调度</button>
        )}
        {status?.as_of && <small>核对于 {dateTime(status.as_of, true)} JST</small>}
        {stale && <button className="scheduler-retry" onClick={() => void refresh()} disabled={pending !== null}>重新读取状态</button>}
      </div>
    </section>
  );
}
