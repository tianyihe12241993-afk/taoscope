"use client";
import { ReactNode, useEffect, useRef, useState } from "react";

/** Reduce a value node to something comparable across renders. Only plain
 *  strings and numbers can be diffed; a composed node is left alone rather
 *  than guessed at, so a tile with markup in it simply never flashes. */
function scalar(v: ReactNode): string | null {
  return typeof v === "string" || typeof v === "number" ? String(v) : null;
}

/** Hero figure — an instrument readout: micro caption, mono value, signed delta.
 *  Mono here (unlike the old proportional treatment) so a tile updating on a
 *  live tick doesn't reflow its own width.
 *
 *  On a live tick the figure flashes its own direction's colour and settles.
 *  The flash paints the background only: nothing resizes or moves, so a tile
 *  updating under the cursor never shifts what is being pointed at. */
export function Stat({
  label, value, sub, delta, hint,
}: {
  label: string; value: ReactNode; sub?: ReactNode; delta?: number | null; hint?: string;
}) {
  const up = (delta ?? 0) >= 0;
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  const prev = useRef<string | null>(scalar(value));

  useEffect(() => {
    const now = scalar(value);
    // First paint is a baseline, never a flash -- otherwise every navigation
    // replays the whole board as if it had just ticked.
    if (now === null || prev.current === null || now === prev.current) {
      prev.current = now;
      return;
    }
    const a = parseFloat(prev.current.replace(/[^0-9.-]/g, ""));
    const b = parseFloat(now.replace(/[^0-9.-]/g, ""));
    // Direction comes from the values themselves when both are numeric; the
    // delta prop describes a period, not this tick.
    const dir = Number.isFinite(a) && Number.isFinite(b) ? (b >= a ? "up" : "down") : up ? "up" : "down";
    prev.current = now;
    setFlash(dir);
    const t = setTimeout(() => setFlash(null), 750);
    return () => clearTimeout(t);
  }, [value, up]);

  return (
    <div className="card px-3.5 py-3 flex flex-col justify-between min-h-[88px]" title={hint}>
      <div className="label-micro">{label}</div>
      <div className="mt-2 flex items-baseline gap-2 flex-wrap">
        <span
          key={flash ?? "idle"}
          className={`tnum text-[22px] leading-none font-semibold tracking-[-0.02em] px-1 -mx-1 ${
            flash === "up" ? "tick-up" : flash === "down" ? "tick-down" : ""
          }`}
          style={{ color: "var(--text-primary)" }}
        >
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
