"use client";
import { useState } from "react";
import { createPortal } from "react-dom";

export interface BurnHistory {
  /** ISO start of the first bucket; every series shares this grid. */
  start: string | null;
  bucket_hours: number;
  /** netuid -> burn % per bucket (0..100), null where there is no sample. */
  series: Record<string, (number | null)[]>;
}

const median = (xs: number[]) => {
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

/** Change in burn over the window, in percentage points: the median of the last
 *  four real buckets minus the median of the first four (~2 days each at 12h).
 *  Not last-minus-first: a single 12-hour blip at either end would otherwise
 *  write the headline -- one subnet read "▼50" off one early bucket while its
 *  line sat at 0% for the whole month. The sparkline still shows the blip. */
export function burnChange(values: (number | null)[] | undefined): number | null {
  if (!values) return null;
  const real = values.filter((v): v is number => v !== null);
  if (real.length < 2) return null;
  const k = Math.min(4, Math.floor(real.length / 2));
  return median(real.slice(-k)) - median(real.slice(0, k));
}

const W = 76;
const H = 24;
const PAD = 3;

/**
 * Burn % over time, drawn on a FIXED 0–100 domain.
 *
 * Not the generic <Spark>: that one min–max scales each row and colours by
 * direction. Auto-scaling would draw 99.0% -> 99.5% as a cliff and make rows
 * incomparable, and for burn "rising" is the bad direction, so green-for-up
 * would say the opposite of the truth. Here every row shares one scale and one
 * time grid, the line wears a neutral ink, and only the latest point carries
 * the accent. Gaps (no sample, or a subnet that paid miners nothing) break the
 * line instead of being bridged.
 */
export function BurnSpark({ values, start, bucketHours }: {
  values: (number | null)[] | undefined; start: string | null; bucketHours: number;
}) {
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);
  const n = values?.length ?? 0;
  const real = values?.filter((v): v is number => v !== null) ?? [];
  if (!values || real.length === 0) {
    return <span style={{ color: "var(--text-muted)" }} title="No burn history yet">—</span>;
  }

  const x = (i: number) => PAD + (n > 1 ? (i / (n - 1)) * (W - 2 * PAD) : (W - 2 * PAD) / 2);
  const y = (v: number) => PAD + (1 - v / 100) * (H - 2 * PAD);

  // Split into runs of consecutive real values; a run of one is drawn as a dot.
  const runs: { i: number; v: number }[][] = [];
  let cur: { i: number; v: number }[] = [];
  values.forEach((v, i) => {
    if (v === null) { if (cur.length) runs.push(cur); cur = []; }
    else cur.push({ i, v });
  });
  if (cur.length) runs.push(cur);

  let lastIdx = -1;
  for (let i = n - 1; i >= 0; i--) if (values[i] !== null) { lastIdx = i; break; }

  const when = (i: number) => {
    if (!start) return "";
    const t = new Date(new Date(start).getTime() + i * bucketHours * 3600_000);
    return t.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };

  const first = real[0];
  const last = real[real.length - 1];
  const label = `Burn over ${Math.round((n * bucketHours) / 24)} days: ${first.toFixed(1)}% to ${last.toFixed(1)}%`;

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const rel = Math.min(1, Math.max(0, (e.clientX - r.left - PAD) / (W - 2 * PAD)));
    const i = Math.round(rel * (n - 1));
    setHover({ i, x: r.left + x(i), y: r.top });
  };

  const hv = hover ? values[hover.i] : null;

  return (
    <>
      <svg width={W} height={H} role="img" aria-label={label} tabIndex={0}
           style={{ display: "block", cursor: "crosshair", overflow: "visible" }}
           onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        {/* recessive frame: the 100% ceiling and 0% floor, so a line hugging
            the top reads as "all of it" without an axis */}
        <line x1={PAD} x2={W - PAD} y1={y(100)} y2={y(100)} stroke="var(--grid)" strokeWidth={1} />
        <line x1={PAD} x2={W - PAD} y1={y(0)} y2={y(0)} stroke="var(--border-strong)" strokeWidth={1} />
        {runs.map((run, k) => run.length === 1 ? (
          <circle key={k} cx={x(run[0].i)} cy={y(run[0].v)} r={1.5} fill="var(--text-secondary)" />
        ) : (
          <path key={k} fill="none" stroke="var(--text-secondary)" strokeWidth={1.75}
                strokeLinejoin="round" strokeLinecap="round"
                d={run.map((p, j) => `${j ? "L" : "M"}${x(p.i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ")} />
        ))}
        {hover && (
          <line x1={x(hover.i)} x2={x(hover.i)} y1={0} y2={H} stroke="var(--border-strong)" strokeWidth={1} />
        )}
        {hover && hv !== null && hv !== undefined && (
          <circle cx={x(hover.i)} cy={y(hv)} r={3} fill="var(--text-primary)"
                  stroke="var(--surface-1)" strokeWidth={1.5} />
        )}
        {lastIdx >= 0 && (
          <circle cx={x(lastIdx)} cy={y(values[lastIdx] as number)} r={3} fill="var(--accent)"
                  stroke="var(--surface-1)" strokeWidth={1.5} />
        )}
      </svg>
      {hover && typeof document !== "undefined" && createPortal(
        <div role="tooltip" style={{
          position: "fixed", left: hover.x, top: hover.y - 8, transform: "translate(-50%, -100%)",
          zIndex: 100, pointerEvents: "none", whiteSpace: "nowrap",
          background: "var(--surface-1)", border: "1px solid var(--border-strong)",
          borderRadius: 8, boxShadow: "var(--shadow-lg)", padding: "5px 9px",
          fontSize: 12, lineHeight: 1.35,
        }}>
          <div style={{ color: "var(--text-muted)", fontSize: 11 }}>{when(hover.i)}</div>
          <div className="tnum" style={{ color: "var(--text-primary)", fontWeight: 600 }}>
            {hv === null || hv === undefined ? "no data" : `${hv.toFixed(1)}% burned`}
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
