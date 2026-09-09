"use client";
import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { ago } from "@/lib/format";

const ICON: Record<string, string> = {
  new_subnet: "🆕", king_change: "👑", registration: "🚪",
  my_miners: "⚠️", emission_move: "📈", price_alert: "🔔",
};

const TONE: Record<string, string> = {
  good: "var(--delta-up)", warn: "var(--serious)", info: "var(--text-secondary)",
};

type Row = Record<string, any>;

/** Renders nothing until there is actually something to report. */
export function ActivityFeed({ limit = 6 }: { limit?: number }) {
  const [rows, setRows] = useState<Row[]>([]);

  useEffect(() => {
    const load = () => api(`/api/events?limit=${limit}`)
      .then((d) => setRows(d.events)).catch(() => {});
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [limit]);

  if (rows.length === 0) return null;

  return (
    <div className="card px-4 py-3 mb-5">
      <div className="flex items-center mb-2">
        <h2 className="text-[12.5px] font-medium">Recent activity</h2>
        <span className="ml-auto text-[11.5px]" style={{ color: "var(--text-muted)" }}>
          new subnets · top-earner changes · registration flips
        </span>
      </div>
      <div className="flex flex-col">
        {rows.map((e) => (
          <div key={e.id} className="flex items-center gap-2.5 py-[5px] text-[12.5px]">
            <span aria-hidden>{ICON[e.kind] ?? "•"}</span>
            {e.netuid !== null && e.netuid !== undefined ? (
              <Link href={`/subnet/${e.netuid}`} className="hover:underline font-medium">
                {e.title}
              </Link>
            ) : (
              <span className="font-medium">{e.title}</span>
            )}
            {e.body && (
              <span className="truncate" style={{ color: TONE[e.severity] ?? "var(--text-secondary)" }}>
                {e.body}
              </span>
            )}
            <span className="ml-auto tnum shrink-0" style={{ color: "var(--text-muted)" }}>
              {ago(e.ts)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
