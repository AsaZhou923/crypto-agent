import { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  TickMarkType,
  CandlestickSeries,
  HistogramSeries,
  AreaSeries,
  createSeriesMarkers,
  type UTCTimestamp,
  type Time,
} from "lightweight-charts";
import type { Decision, Equity, Fill, Market } from "./types";
import { dateTime } from "./format";
import { marketMarkers, marketPrecision, marketPrice } from "./market";
const stamp = (iso: string) =>
  Math.floor(Date.parse(iso) / 1000) as UTCTimestamp;
const options = (height: number) => ({
  height,
  layout: {
    background: { type: ColorType.Solid, color: "#13171e" },
    textColor: "#8792a2",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 11,
    attributionLogo: false,
  },
  grid: { vertLines: { color: "#1c222b" }, horzLines: { color: "#1c222b" } },
  rightPriceScale: { borderColor: "#29313c" },
  timeScale: {
    borderColor: "#29313c",
    timeVisible: true,
    secondsVisible: false,
    tickMarkFormatter: (time: Time, tick: TickMarkType) =>
      typeof time === "number"
        ? new Intl.DateTimeFormat("en-GB", {
            timeZone: "Asia/Tokyo",
            ...(tick === TickMarkType.Time ||
            tick === TickMarkType.TimeWithSeconds
              ? {
                  hour: "2-digit" as const,
                  minute: "2-digit" as const,
                  hourCycle: "h23" as const,
                }
              : { month: "2-digit" as const, day: "2-digit" as const }),
          }).format(new Date(time * 1000))
        : String(time),
  },
  localization: {
    locale: "en-US",
    timeFormatter: (time: Time) =>
      typeof time === "number"
        ? `${dateTime(new Date(time * 1000).toISOString())} JST`
        : String(time),
  },
});
export function MarketChart({
  data,
  fills,
  decisions,
}: {
  data: Market;
  fills: Fill[];
  decisions: Decision[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current || !data.bars.length) return;
    const chart = createChart(ref.current, {
      ...options(315),
      width: ref.current.clientWidth,
    });
    const series = chart.addSeries(CandlestickSeries, {
      priceFormat: { type: "price", precision: marketPrecision(data.symbol), minMove: 10 ** -marketPrecision(data.symbol) },
      upColor: "#64c7a3",
      downColor: "#dc7b86",
      borderVisible: false,
      wickUpColor: "#64c7a3",
      wickDownColor: "#dc7b86",
    });
    series
      .priceScale()
      .applyOptions({ scaleMargins: { top: 0.13, bottom: 0.26 } });
    const bars = data.bars.map((b) => ({
      time: stamp(b.time),
      open: +b.open,
      high: +b.high,
      low: +b.low,
      close: +b.close,
    }));
    series.setData(bars);
    const volumes = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      priceLineVisible: false,
      lastValueVisible: false,
    });
    volumes
      .priceScale()
      .applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
    volumes.setData(
      data.bars.map((b) => ({
        time: stamp(b.time),
        value: +b.volume,
        color: +b.close >= +b.open ? "#294c42" : "#54343c",
      })),
    );
    createSeriesMarkers(series, marketMarkers(data, fills, decisions).map((marker) => ({
      ...marker, time: marker.time as UTCTimestamp,
    })));
    chart.timeScale().fitContent();
    const observer = new ResizeObserver(() => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth });
    });
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [data, fills, decisions]);
  return (
    <div
      ref={ref}
      className="market-canvas"
      role="img"
      aria-label={`${data.symbol} ${data.timeframe} K线和成交量，${data.bars.length}根。买卖箭头为实际成交，紫色圆点为Agent信号。末根收盘价${marketPrice(data.bars.at(-1)?.close, data.symbol)}`}
    />
  );
}
export function EquityChart({ data }: { data: Equity }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current || data.points.length < 2) return;
    const chart = createChart(ref.current, {
      ...options(142),
      width: ref.current.clientWidth,
    });
    const series = chart.addSeries(AreaSeries, {
      lineColor: "#9ab4d1",
      topColor: "#9ab4d118",
      bottomColor: "#9ab4d102",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
    });
    // SQLite can contain observations in the same second; keep the last value per chart time.
    const points = new Map(
      data.points.map((p) => [
        stamp(p.time),
        { time: stamp(p.time), value: +p.value },
      ]),
    );
    series.setData([...points.values()].sort((a, b) => a.time - b.time));
    chart.timeScale().fitContent();
    const observer = new ResizeObserver(() => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth });
    });
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [data]);
  return (
    <div
      ref={ref}
      role="img"
      aria-label={`账户权益历史，共${data.points.length}次观察，非BTC价格或收益曲线`}
    />
  );
}
