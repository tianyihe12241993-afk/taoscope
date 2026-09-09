"use client";
import { ReactNode } from "react";

/** Hero figure — an instrument readout: micro caption, mono value, signed delta.
 *  Mono here (unlike the old proportional treatment) so a tile updating on a
 *  live tick doesn't reflow its own width. */
export function Stat({
  label, value, sub, delta, hint,
}: {
  label: string; value: ReactNode; sub?: ReactNode; delta?: number | null; hint?: string;
}) {
  const up = (delta ?? 0) >= 0;
  return (
    <div className="card px-3.5 py-3 flex flex-col justify-between min-h-[88px]" title={hint}>
      <div className="label-micro">{label}</div>
      <div className="mt-2 flex items-baseline gap-2 flex-wrap">
        <span className="tnum text-[21px] leading-none font-semibold tracking-[-0.02em]"
              style={{ color: "var(--text-primary)" }}>
          {value}
        </span>
        {delta !== undefined && delta !== null && (
          <span className="tnum text-[11.5px] inline-flex items-center gap-0.5 font-semibold"
                style={{ color: up ? "var(--delta-up)" : "var(--delta-down)" }}>
            <span aria-hidden>{up ? "▲" : "▼"}</span>{Math.abs(delta).toFixed(2)}%
          </span>
        )}
      </div>
      {sub ? (
        <div className="mt-1.5 text-[11.5px] leading-tight" style={{ color: "var(--text-secondary)" }}>
          {sub}
        </div>
      ) : <div className="mt-1.5" />}
    </div>
  );
}
