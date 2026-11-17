// Small dependency-free charts for the statistics page (SVG + CSS grid).
// Colours come from the skin's CSS variables, so they follow both skins and dark mode.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";

export const SERIES_COLORS = ["var(--accent)", "var(--info)", "var(--ok)", "var(--warn)", "#8e5bd8", "#0b8a8a", "#c2410c", "var(--ink-3)"];

const nf = new Intl.NumberFormat("en");
export const num = (n: number | null | undefined) => (n == null ? "—" : nf.format(n));
export function compact(n: number): string {
  if (Math.abs(n) < 1000) return nf.format(Math.round(n));
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

function useWidth<T extends HTMLElement>(): [RefObject<T | null>, number] {
  const ref = useRef<T | null>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setW(Math.round(e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

/* ------------------------------------------------------------------ time chart */

export interface TimePoint {
  key: string;
  label: string; // axis label
  long: string; // tooltip title
  value: number;
  line?: number; // optional overlay (e.g. cumulative)
}

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}

export function TimeChart({ points, format, lineFormat, lineLabel, onSelect, height = 220 }: {
  points: TimePoint[];
  format: (n: number) => string;
  lineFormat?: (n: number) => string;
  lineLabel?: string;
  onSelect?: (key: string) => void;
  height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hoverIdx, setHover] = useState<number | null>(null);
  // The hovered index can outlive its data: clicking a bar zooms in and the new
  // timeline has fewer points, so ignore an index that no longer exists.
  const hover = hoverIdx != null && hoverIdx < points.length ? hoverIdx : null;
  const pad = { l: 44, r: points.some((p) => p.line != null) ? 48 : 10, t: 12, b: 26 };
  const w = Math.max(width, 200);
  const iw = w - pad.l - pad.r;
  const ih = height - pad.t - pad.b;
  const max = niceMax(Math.max(0, ...points.map((p) => p.value)));
  const lmax = niceMax(Math.max(0, ...points.map((p) => p.line ?? 0)));
  const n = Math.max(points.length, 1);
  const slot = iw / n;
  const bw = Math.max(2, Math.min(36, slot * 0.72));
  const y = (v: number) => pad.t + ih - (v / max) * ih;
  const ly = (v: number) => pad.t + ih - (v / lmax) * ih;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max);
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 56))));
  const hasLine = points.some((p) => p.line != null);
  const linePath = hasLine
    ? points.map((p, i) => `${i ? "L" : "M"}${pad.l + slot * i + slot / 2},${ly(p.line ?? 0)}`).join("")
    : "";
  const hp = hover != null ? points[hover] : null;

  return (
    <div className="chart" ref={ref} onMouseLeave={() => setHover(null)}>
      {width > 0 && (
        <svg width={w} height={height} role="img" aria-label="Activity over time">
          {ticks.map((t) => (
            <g key={t}>
              <line x1={pad.l} x2={w - pad.r} y1={y(t)} y2={y(t)} className="grid" />
              <text x={pad.l - 6} y={y(t) + 4} textAnchor="end" className="axis">{compact(t)}</text>
            </g>
          ))}
          {hasLine && [0, 0.5, 1].map((f) => (
            <text key={f} x={w - pad.r + 6} y={ly(f * lmax) + 4} className="axis line-axis">{compact(f * lmax)}</text>
          ))}
          {points.map((p, i) => {
            const x = pad.l + slot * i + (slot - bw) / 2;
            const h = Math.max(p.value > 0 ? 2 : 0, pad.t + ih - y(p.value));
            return (
              <g key={p.key}>
                <rect
                  x={pad.l + slot * i} y={pad.t} width={slot} height={ih} className="hit"
                  onMouseEnter={() => setHover(i)} onClick={() => { setHover(null); onSelect?.(p.key); }}
                  onTouchStart={() => setHover(i)}
                  style={{ cursor: onSelect ? "zoom-in" : "default" }}
                />
                <rect x={x} y={pad.t + ih - h} width={bw} height={h} rx={Math.min(4, bw / 3)}
                  className={`bar${hover === i ? " on" : ""}`} pointerEvents="none" />
                {i % every === 0 && (
                  <text x={pad.l + slot * i + slot / 2} y={height - 8} textAnchor="middle" className="axis">{p.label}</text>
                )}
              </g>
            );
          })}
          {hasLine && <path d={linePath} className="line" pointerEvents="none" />}
          {hasLine && hover != null && (
            <circle cx={pad.l + slot * hover + slot / 2} cy={ly(points[hover].line ?? 0)} r={4} className="line-dot" pointerEvents="none" />
          )}
        </svg>
      )}
      {hp && hover != null && (
        <div
          className="chart-tip"
          style={{ left: Math.min(Math.max(pad.l + slot * hover + slot / 2, 70), w - 70), top: 4 }}
        >
          <strong>{hp.long}</strong>
          <span>{format(hp.value)}</span>
          {hp.line != null && <span className="muted">{lineLabel}: {(lineFormat ?? format)(hp.line)}</span>}
          {onSelect && <span className="muted small">click to zoom</span>}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ horizontal bars */

export interface BarItem {
  key: string;
  label: ReactNode;
  value: number;
  sub?: string;
  color?: string;
  active?: boolean;
}

export function BarList({ items, format, onToggle, limit = 12, empty = "No data in this period." }: {
  items: BarItem[];
  format: (n: number) => string;
  onToggle?: (key: string) => void;
  limit?: number;
  empty?: string;
}) {
  const [all, setAll] = useState(false);
  useEffect(() => setAll(false), [items.length]);
  if (items.length === 0) return <div className="muted small">{empty}</div>;
  const max = Math.max(1, ...items.map((i) => i.value));
  const shown = all ? items : items.slice(0, limit);
  return (
    <div className="barlist">
      {shown.map((it) => {
        const body = (
          <>
            <div className="bl-head">
              <span className="bl-label ellipsis">{it.label}</span>
              <span className="bl-val">{format(it.value)}{it.sub && <span className="muted"> · {it.sub}</span>}</span>
            </div>
            <div className="bl-track"><span style={{ width: `${(it.value / max) * 100}%`, background: it.color }} /></div>
          </>
        );
        return onToggle ? (
          <button key={it.key} type="button" className={`bl-row${it.active ? " active" : ""}`} aria-pressed={!!it.active}
            onClick={() => onToggle(it.key)} title={it.active ? "Remove filter" : "Filter by this"}>
            {body}
          </button>
        ) : (
          <div key={it.key} className="bl-row">{body}</div>
        );
      })}
      {items.length > limit && (
        <button type="button" className="btn sm ghost" onClick={() => setAll(!all)}>
          {all ? "Show less" : `Show all ${items.length}`}
        </button>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ donut */

export function Donut({ items, format, onToggle, centre }: {
  items: BarItem[];
  format: (n: number) => string;
  onToggle?: (key: string) => void;
  centre?: string;
}) {
  const [hover, setHover] = useState<string | null>(null);
  const total = items.reduce((a, b) => a + b.value, 0);
  const r = 42;
  const c = 2 * Math.PI * r;
  let acc = 0;
  const h = items.find((i) => i.key === hover);
  return (
    <div className="donut">
      <svg viewBox="0 0 120 120" width={120} height={120} role="img" aria-label="Breakdown">
        <circle cx={60} cy={60} r={r} className="donut-bg" />
        {total > 0 && items.map((it, i) => {
          const len = (it.value / total) * c;
          const el = (
            <circle
              key={it.key} cx={60} cy={60} r={r} fill="none"
              stroke={it.color ?? SERIES_COLORS[i % SERIES_COLORS.length]}
              strokeWidth={hover === it.key || it.active ? 20 : 15}
              strokeDasharray={`${len} ${c - len}`} strokeDashoffset={-acc}
              transform="rotate(-90 60 60)"
              style={{ cursor: onToggle ? "pointer" : "default", transition: "stroke-width .15s" }}
              onMouseEnter={() => setHover(it.key)} onMouseLeave={() => setHover(null)}
              onClick={() => onToggle?.(it.key)}
            />
          );
          acc += len;
          return el;
        })}
        <text x={60} y={58} textAnchor="middle" className="donut-num">{h ? format(h.value) : centre ?? format(total)}</text>
        <text x={60} y={74} textAnchor="middle" className="donut-lbl">
          {h ? `${total ? Math.round((h.value / total) * 100) : 0}%` : "total"}
        </text>
      </svg>
      <div className="legend">
        {items.map((it, i) => (
          <button key={it.key} type="button" className={`lg${it.active ? " active" : ""}`} aria-pressed={!!it.active}
            disabled={!onToggle} onClick={() => onToggle?.(it.key)}
            onMouseEnter={() => setHover(it.key)} onMouseLeave={() => setHover(null)}>
            <span className="sw" style={{ background: it.color ?? SERIES_COLORS[i % SERIES_COLORS.length] }} />
            <span className="ellipsis">{it.label}</span>
            <span className="muted">{format(it.value)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ heatmap */

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function Heatmap({ data }: { data: number[][] }) {
  const max = Math.max(1, ...data.flat());
  return (
    <div className="heatmap" role="table" aria-label="Downloads by weekday and hour">
      <div className="hm-row hm-head" role="row">
        <span />
        {Array.from({ length: 24 }, (_, h) => (
          <span key={h} className="hm-hour">{h % 3 === 0 ? h : ""}</span>
        ))}
      </div>
      {data.map((row, d) => (
        <div className="hm-row" role="row" key={d}>
          <span className="hm-day">{DAYS[d]}</span>
          {row.map((v, h) => (
            <span
              key={h}
              role="cell"
              className="hm-cell"
              title={`${DAYS[d]} ${String(h).padStart(2, "0")}:00 – ${v} chapter${v === 1 ? "" : "s"}`}
              style={{ opacity: v === 0 ? 0.08 : 0.2 + 0.8 * (v / max) }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ segmented control */

export function Seg<T extends string>({ value, onChange, items, label }: {
  value: T;
  onChange: (v: T) => void;
  items: { value: T; label: string }[];
  label?: string;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {items.map((it) => (
        <button key={it.value} type="button" aria-pressed={value === it.value} onClick={() => onChange(it.value)}>
          {it.label}
        </button>
      ))}
    </div>
  );
}
