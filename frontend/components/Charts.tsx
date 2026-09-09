"use client";
import { useEffect, useRef, useState } from "react";

/* ---------- shared sizing ---------- */
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

const AXIS = "var(--axis)";
const GRID = "var(--grid)";
const MUTED = "var(--text-muted)";

/* ---------- line / area time series ---------- */
export interface Point { ts: string | number; v: number }

export function LineChart({
  data, height = 260, color = "var(--series-1)", label, valueFmt = (n) => n.toFixed(4), area = true,
}: {
  data: Point[]; height?: number; color?: string; label?: string;
  valueFmt?: (n: number) => string; area?: boolean;
}) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const padL = 56, padR = 12, padT = 12, padB = 24;

  if (data.length === 0) {
    return (
      <div ref={ref} className="flex items-center justify-center text-[13px]"
           style={{ height, color: MUTED }}>
        collecting data…
      </div>
    );
  }

  const iw = Math.max(1, w - padL - padR);
  const ih = height - padT - padB;
  const xs = data.map((d) => new Date(d.ts).getTime());
  const ys = data.map((d) => d.v);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= y0 * 0.01 || 1; y1 += y1 * 0.01 || 1; }
  const pad = (y1 - y0) * 0.08;
  const allNonNegative = Math.min(...ys) >= 0;
  y0 -= pad;
  y1 += pad;
  // never draw a negative axis for a quantity that cannot be negative
  if (allNonNegative && y0 < 0) y0 = 0;

  const px = (t: number) => padL + ((t - x0) / (x1 - x0 || 1)) * iw;
  const py = (v: number) => padT + ih - ((v - y0) / (y1 - y0 || 1)) * ih;

  const line = data.map((d, i) => `${i ? "L" : "M"}${px(xs[i])},${py(d.v)}`).join(" ");
  const fill = `${line} L${px(xs[xs.length - 1])},${padT + ih} L${px(xs[0])},${padT + ih} Z`;
  const ticks = 4;
  const gid = `grad-${label?.replace(/\W/g, "") ?? "s"}`;

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    let best = 0, bd = Infinity;
    xs.forEach((t, i) => { const d = Math.abs(px(t) - mx); if (d < bd) { bd = d; best = i; } });
    setHover(best);
  };

  const h = hover !== null ? data[hover] : null;

  return (
    <div ref={ref} className="relative" style={{ height }}>
      {w > 0 && (
        <svg width={w} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity="0.22" />
              <stop offset="100%" stopColor={color} stopOpacity="0" />
            </linearGradient>
          </defs>

          {Array.from({ length: ticks + 1 }, (_, i) => {
            const v = y0 + ((y1 - y0) * i) / ticks;
            const y = py(v);
            return (
              <g key={i}>
                <line x1={padL} x2={w - padR} y1={y} y2={y} stroke={GRID} strokeWidth={1} />
                <text x={padL - 8} y={y + 3.5} textAnchor="end" fontSize={10} fill={MUTED} className="tnum">
                  {valueFmt(v)}
                </text>
              </g>
            );
          })}
          <line x1={padL} x2={w - padR} y1={padT + ih} y2={padT + ih} stroke={AXIS} strokeWidth={1} />

          {area && <path d={fill} fill={`url(#${gid})`} />}
          <path d={line} fill="none" stroke={color} strokeWidth={2}
                strokeLinejoin="round" strokeLinecap="round" />

          {[0, Math.floor(data.length / 2), data.length - 1].map((i) => (
            <text key={i} x={px(xs[i])} y={height - 6} fontSize={10} fill={MUTED}
                  textAnchor={i === 0 ? "start" : i === data.length - 1 ? "end" : "middle"}>
              {new Date(xs[i]).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </text>
          ))}

          {h && (
            <g pointerEvents="none">
              <line x1={px(xs[hover!])} x2={px(xs[hover!])} y1={padT} y2={padT + ih}
                    stroke={AXIS} strokeWidth={1} />
              <circle cx={px(xs[hover!])} cy={py(h.v)} r={4.5} fill={color}
                      stroke="var(--surface-1)" strokeWidth={2} />
            </g>
          )}
        </svg>
      )}
      {h && (
        <div
          className="absolute pointer-events-none card px-2.5 py-1.5 text-[12px]"
          style={{
            left: Math.min(Math.max(px(xs[hover!]) + 10, 4), Math.max(4, w - 150)),
            top: 8, boxShadow: "var(--shadow-lg)",
          }}
        >
          <div className="tnum font-medium">{valueFmt(h.v)}</div>
          <div className="tnum" style={{ color: MUTED }}>
            {new Date(h.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- horizontal magnitude bars ---------- */
export function BarList({
  items, valueFmt = (n) => n.toFixed(2), color = "var(--series-1)", max, onClick,
}: {
  items: { label: string; value: number; hint?: string; href?: string }[];
  valueFmt?: (n: number) => string; color?: string; max?: number;
  onClick?: (label: string) => void;
}) {
  const top = max ?? Math.max(...items.map((i) => i.value), 0.0001);
  return (
    <div className="flex flex-col gap-1.5">
      {items.map((it) => (
        <div
          key={it.label}
          className="group grid items-center gap-3 text-[12px] cursor-default"
          style={{ gridTemplateColumns: "120px 1fr 84px" }}
          title={it.hint}
          onClick={() => onClick?.(it.label)}
        >
          <div className="truncate" style={{ color: "var(--text-secondary)" }}>{it.label}</div>
          <div className="relative h-[14px] rounded-sm" style={{ background: "rgba(127,127,127,0.10)" }}>
            <div
              className="absolute inset-y-0 left-0 transition-[width]"
              style={{
                width: `${Math.max(1, (it.value / top) * 100)}%`,
                background: color,
                borderTopRightRadius: 4, borderBottomRightRadius: 4,
              }}
            />
          </div>
          <div className="tnum text-right">{valueFmt(it.value)}</div>
        </div>
      ))}
    </div>
  );
}

/* ---------- concentration: stacked share bar ---------- */
const SERIES = [1, 2, 3, 4, 5, 6, 7, 8].map((i) => `var(--series-${i})`);

export function StackBar({
  segments, height = 26,
}: {
  segments: { label: string; value: number }[]; height?: number;
}) {
  const total = segments.reduce((a, b) => a + b.value, 0) || 1;
  return (
    <div>
      <div className="flex w-full overflow-hidden rounded-md" style={{ height }}>
        {segments.map((s, i) => (
          <div
            key={s.label + i}
            title={`${s.label} — ${((s.value / total) * 100).toFixed(1)}%`}
            style={{
              width: `${(s.value / total) * 100}%`,
              background: s.label === "Other" ? "var(--axis)" : SERIES[i % SERIES.length],
              // 2px surface gap between adjacent segments
              marginRight: i < segments.length - 1 ? 2 : 0,
            }}
          />
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
        {segments.map((s, i) => (
          <span key={s.label + i} className="inline-flex items-center gap-1.5"
                style={{ color: "var(--text-secondary)" }}>
            <span className="w-2.5 h-2.5 rounded-sm inline-block"
                  style={{ background: s.label === "Other" ? "var(--axis)" : SERIES[i % SERIES.length] }} />
            {s.label}
            <span className="tnum" style={{ color: "var(--text-muted)" }}>
              {((s.value / total) * 100).toFixed(1)}%
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}

/* ---------- inline sparkline (no axes; trend only) ---------- */
export function Spark({ values, w = 90, h = 22, color = "var(--series-1)" }: {
  values: number[]; w?: number; h?: number; color?: string;
}) {
  if (values.length < 2) return <span style={{ color: MUTED }}>—</span>;
  const lo = Math.min(...values), hi = Math.max(...values);
  const d = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * (w - 2) + 1;
      const y = h - 1 - ((v - lo) / (hi - lo || 1)) * (h - 2);
      return `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const rising = values[values.length - 1] >= values[0];
  return (
    <svg width={w} height={h} style={{ display: "block" }}>
      <path d={d} fill="none" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round"
            stroke={rising ? "var(--delta-up)" : "var(--delta-down)"} />
    </svg>
  );
}
