"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Chrome } from "@/components/Chrome";
import { PageHeader } from "@/components/PageHeader";
import { Col, DataTable } from "@/components/Table";
import { api } from "@/lib/api";
import { compact, key } from "@/lib/format";
import { useLive } from "@/lib/live";
import { Pill, useCurrency } from "@/lib/ui";

type Row = Record<string, any>;
type Role = "miner" | "validator" | "all";

const TABS: { id: Role; label: string; blurb: string }[] = [
  { id: "miner", label: "Miners", blurb: "Ranked by what their miner hotkeys earn — validator income excluded." },
  { id: "validator", label: "Validators", blurb: "Ranked by validator income. A handful of coldkeys, most of the emission." },
  { id: "all", label: "All", blurb: "Every coldkey by total earnings, both roles combined." },
];

export default function Operators() {
  const live = useLive();
  const { money, moneyCompact } = useCurrency();
  const [role, setRole] = useState<Role>("miner");
  const [rows, setRows] = useState<Row[]>([]);
  const [totals, setTotals] = useState<Row>({});
  const [q, setQ] = useState("");

  const load = useCallback(() => {
    api(`/api/coldkeys/top?role=${role}&limit=300`)
      .then((d) => { setRows(d.coldkeys); setTotals(d.totals ?? {}); })
      .catch(() => {});
  }, [role]);

  useEffect(() => {
    load();
    const t = setInterval(load, 120_000);
    return () => clearInterval(t);
  }, [load]);

  const filtered = rows.filter((r) => {
    const term = q.trim().toLowerCase();
    if (!term) return true;
    return r.coldkey.toLowerCase().includes(term) || (r.label ?? "").toLowerCase().includes(term);
  });

  // the earnings column follows the tab, so each view ranks by what it's about
  const earnKey = role === "validator" ? "validator_tao_per_day"
                : role === "miner" ? "miner_tao_per_day" : "tao_per_day";

  const cols: Col<Row>[] = [
    { key: "coldkey", label: "Coldkey", value: (r) => r.coldkey, defaultDesc: false,
      render: (r) => (
        <span className="flex items-center gap-2">
          <Link href={`/operators/${r.coldkey}`} className="hover:underline tnum"
                style={{ color: "var(--accent)" }}>
            {r.label || key(r.coldkey, 12, 8)}
          </Link>
          {r.is_ours && <Pill tone="accent">mine</Pill>}
        </span>
      ) },
    { key: earnKey, label: role === "all" ? "Earning / day" : `${role === "miner" ? "Mining" : "Validating"} / day`,
      align: "right", value: (r) => r[earnKey] ?? 0,
      render: (r) => <span className="tnum font-medium">{money(r[earnKey], 2)}</span> },
    { key: "monthly", label: "/ month", align: "right", value: (r) => (r[earnKey] ?? 0) * 30,
      render: (r) => <span className="tnum" style={{ color: "var(--text-secondary)" }}>
        {moneyCompact((r[earnKey] ?? 0) * 30, 1)}</span> },
    { key: "miner_hotkeys", label: "Miner hk", align: "right", value: (r) => r.miner_hotkeys ?? 0,
      title: "Hotkeys without a validator permit",
      render: (r) => <span className="tnum">{r.miner_hotkeys ?? 0}</span> },
    { key: "validator_hotkeys", label: "Vali hk", align: "right", value: (r) => r.validator_hotkeys ?? 0,
      title: "Hotkeys holding a validator permit",
      render: (r) => <span className="tnum" style={{ color: (r.validator_hotkeys ?? 0) > 0 ? "var(--series-7)" : "var(--text-muted)" }}>
        {r.validator_hotkeys ?? 0}</span> },
    { key: "subnets", label: "Subnets", align: "right", value: (r) => r.subnets,
      render: (r) => <span className="tnum">{r.subnets}</span> },
    { key: "stake", label: "Stake", align: "right", value: (r) => r.stake ?? 0,
      render: (r) => <span className="tnum">{compact(r.stake, 1)}</span> },
    { key: "netuids", label: "Where",
      render: (r) => <span className="tnum text-[11.5px]" style={{ color: "var(--text-muted)" }}>
        {(r.netuids ?? []).slice(0, 12).join(", ")}{(r.netuids ?? []).length > 12 ? " …" : ""}</span> },
  ];

  const tab = TABS.find((t) => t.id === role)!;

  return (
    <Chrome block={live.block} taoUsd={live.taoUsd} connected={live.connected}>
      <PageHeader
        title="Operators"
        subtitle="Every coldkey on the network. Click one to see all its hotkeys."
      />

      <div className="card">
        <div className="flex items-center gap-1 px-4 pt-3 border-b" style={{ borderColor: "var(--border)" }}>
          {TABS.map((t) => {
            const count = totals[`${t.id}_operators`];
            const active = role === t.id;
            return (
              <button key={t.id} onClick={() => setRole(t.id)}
                      className="px-3 py-2 text-[13px] font-medium relative"
                      style={{ color: active ? "var(--accent)" : "var(--text-secondary)" }}>
                {t.label}
                {count !== undefined && (
                  <span className="ml-1.5 tnum text-[11px]" style={{ color: "var(--text-muted)" }}>
                    {compact(count, 0)}
                  </span>
                )}
                {active && (
                  <span className="absolute left-2 right-2 -bottom-px h-[2px] rounded-full"
                        style={{ background: "var(--accent)" }} />
                )}
              </button>
            );
          })}
          <div className="ml-auto flex items-center gap-3 pb-2">
            <input className="input w-[280px]" placeholder="Search coldkey or label…"
                   value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        </div>

        <div className="px-4 py-2 text-[12px] border-b"
             style={{ color: "var(--text-muted)", borderColor: "var(--border)" }}>
          {tab.blurb}
        </div>

        <DataTable rows={filtered} cols={cols} initialSort={earnKey}
                   rowKey={(r) => r.coldkey} maxHeight={720} tieBreak={(r) => r.coldkey} />
      </div>
    </Chrome>
  );
}
