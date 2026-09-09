"use client";
import { ReactNode } from "react";

/** Hero figure. Proportional digits by design — these are standalone numbers. */
export function Stat({
  label, value, sub, delta, hint,
}: {
  label: string; value: ReactNode; sub?: ReactNode; delta?: number | null; hint?: string;
}) {
  const up = (delta ?? 0) >= 0;
  return (
    <div className="card px-4 py-3.5 flex flex-col justify-between min-h-[92px]" title={hint}>
      <div className="text-[11.5px] font-medium" style={{ color: "var(--text-muted)" }}>
        {label}
      </div>
      <div className="mt-2 flex items-baseline gap-2 flex-wrap">
        <span className="text-[22px] leading-none font-semibold tracking-[-0.02em]">{value}</span>
        {delta !== undefined && delta !== null && (
          <span className="tnum text-[12px] inline-flex items-center gap-0.5 font-medium"
                style={{ color: up ? "var(--delta-up)" : "var(--delta-down)" }}>
            <span aria-hidden>{up ? "▲" : "▼"}</span>{Math.abs(delta).toFixed(2)}%
          </span>
        )}
      </div>
      {sub ? (
        <div className="mt-1.5 text-[12px] leading-tight" style={{ color: "var(--text-secondary)" }}>
          {sub}
        </div>
      ) : <div className="mt-1.5" />}
    </div>
  );
}
