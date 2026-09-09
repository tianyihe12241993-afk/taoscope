"use client";
import { createContext, useCallback, useContext, useEffect, useState } from "react";

import { compact, fmt } from "./format";

/* ---------- requirement 8: one switch flips every monetary figure ---------- */
type Unit = "TAO" | "USD";

interface CurrencyCtx {
  unit: Unit;
  taoUsd: number;
  toggle: () => void;
  setTaoUsd: (n: number) => void;
  /** Render a TAO-denominated value in the active unit. */
  money: (tao: number | null | undefined, decimals?: number) => string;
  /** Same, but compacted (1.2k / 3.4M). */
  moneyCompact: (tao: number | null | undefined, decimals?: number) => string;
  symbol: string;
}

const Ctx = createContext<CurrencyCtx | null>(null);

export function CurrencyProvider({ children }: { children: React.ReactNode }) {
  const [unit, setUnit] = useState<Unit>("TAO");
  const [taoUsd, setTaoUsd] = useState(0);

  useEffect(() => {
    const saved = localStorage.getItem("taoscope-unit");
    if (saved === "USD" || saved === "TAO") setUnit(saved);
  }, []);

  const toggle = useCallback(() => {
    setUnit((u) => {
      const next = u === "TAO" ? "USD" : "TAO";
      localStorage.setItem("taoscope-unit", next);
      return next;
    });
  }, []);

  const money = useCallback(
    (v: number | null | undefined, d = 3) => {
      if (v === null || v === undefined || Number.isNaN(v)) return "—";
      if (unit === "USD") return taoUsd ? `$${fmt(v * taoUsd, 2)}` : "—";
      return `τ${fmt(v, d)}`;
    },
    [unit, taoUsd],
  );

  const moneyCompact = useCallback(
    (v: number | null | undefined, d = 1) => {
      if (v === null || v === undefined || Number.isNaN(v)) return "—";
      if (unit === "USD") return taoUsd ? `$${compact(v * taoUsd, d)}` : "—";
      return `τ${compact(v, d)}`;
    },
    [unit, taoUsd],
  );

  return (
    <Ctx.Provider value={{
      unit, taoUsd, toggle, setTaoUsd, money, moneyCompact,
      symbol: unit === "USD" ? "$" : "τ",
    }}>
      {children}
    </Ctx.Provider>
  );
}

export function useCurrency(): CurrencyCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useCurrency must be used inside CurrencyProvider");
  return c;
}

/* ---------- small shared bits ---------- */
export function Toggle({ options, value, onChange }: {
  options: { value: string; label: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="inline-flex p-0.5 rounded-lg" style={{ background: "var(--surface-2)" }}>
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className="px-2.5 py-1 rounded-[7px] text-[12px] font-medium transition-colors"
          style={{
            background: value === o.value ? "var(--surface-1)" : "transparent",
            color: value === o.value ? "var(--text-primary)" : "var(--text-muted)",
            boxShadow: value === o.value ? "var(--shadow-sm)" : "none",
          }}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Pill({ tone = "neutral", children }: {
  tone?: "neutral" | "good" | "warn" | "bad" | "accent";
  children: React.ReactNode;
}) {
  const map = {
    neutral: ["var(--surface-2)", "var(--text-secondary)"],
    good: ["color-mix(in srgb, var(--good) 14%, transparent)", "var(--delta-up)"],
    warn: ["color-mix(in srgb, var(--warning) 18%, transparent)", "var(--serious)"],
    bad: ["color-mix(in srgb, var(--critical) 14%, transparent)", "var(--delta-down)"],
    accent: ["var(--accent-soft)", "var(--accent)"],
  }[tone];
  return (
    <span className="text-[11px] px-2 py-[3px] rounded-full font-medium"
          style={{ background: map[0], color: map[1] }}>
      {children}
    </span>
  );
}
