"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { StackBar } from "@/components/Charts";
import { Chrome } from "@/components/Chrome";
import { Stat } from "@/components/Stat";
import { Col, DataTable } from "@/components/Table";
import { api, post, put } from "@/lib/api";
import { compact, fmt, key, pct } from "@/lib/format";
import { useLive } from "@/lib/live";
import { Pill, useCurrency } from "@/lib/ui";

type Row = Record<string, any>;

export default function OperatorDetail() {
  const p = useParams();
  const coldkey = String(p.coldkey);
  const live = useLive();
  const { money, moneyCompact } = useCurrency();
  const [d, setD] = useState<Row | null>(null);
  const [label, setLabel] = useState("");
  const [mine, setMine] = useState(false);
  const [saved, setSaved] = useState(false);

  const load = useCallback(() => {
    api(`/api/coldkeys/${coldkey}`).then((r) => {
      setD(r);
      setLabel(r.label?.label ?? "");
      setMine(!!r.label?.is_ours);
    }).catch(() => setD(null));
  }, [coldkey]);

  useEffect(() => { load(); }, [load]);

  const save = async () => {
    await put(`/api/coldkeys/${coldkey}/label`, { label, is_ours: mine });
    if (mine) await post("/api/me/coldkeys", { coldkey, label: label || null }).catch(() => {});
    setSaved(true); setTimeout(() => setSaved(false), 1800);
    load();
  };

  const cols: Col<Row>[] = [
    { key: "netuid", label: "SN", width: 56, align: "right", value: (r) => r.netuid, defaultDesc: false,
      render: (r) => <span className="tnum" style={{ color: "var(--text-muted)" }}>{r.netuid}</span> },
    { key: "subnet_name", label: "Subnet", value: (r) => r.subnet_name ?? "", defaultDesc: false,
      render: (r) => <Link href={`/subnet/${r.netuid}`} className="hover:underline font-medium">
        {r.subnet_name}</Link> },
    { key: "uid", label: "UID", align: "right", value: (r) => r.uid, defaultDesc: false,
      render: (r) => <span className="tnum">{r.uid}</span> },
    { key: "rank_in_subnet", label: "Rank", align: "right", value: (r) => r.rank_in_subnet,
      render: (r) => <span className="tnum">#{r.rank_in_subnet}</span> },
    { key: "tao_per_day", label: "Earning / day", align: "right", value: (r) => r.tao_per_day ?? 0,
      render: (r) => <span className="tnum font-medium">{money(r.tao_per_day, 3)}</span> },
    { key: "emission_pct", label: "Emission %", align: "right", value: (r) => r.emission_pct ?? 0,
      render: (r) => <span className="tnum">{pct(r.emission_pct, 2)}</span> },
    { key: "stake", label: "Stake", align: "right", value: (r) => r.stake ?? 0,
      render: (r) => <span className="tnum">{compact(r.stake, 1)}</span> },
    { key: "hotkey", label: "Hotkey", value: (r) => r.hotkey,
      render: (r) => <span className="tnum text-[11.5px]" style={{ color: "var(--text-muted)" }}>
        {key(r.hotkey, 10, 6)}</span> },
    { key: "validator_permit", label: "Role", width: 72, value: (r) => (r.validator_permit ? 1 : 0),
      render: (r) => r.validator_permit ? <Pill tone="accent">validator</Pill>
        : <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>miner</span> },
  ];

  const segs = (() => {
    if (!d) return [];
    const m = new Map<number, { label: string; value: number }>();
    for (const q of d.positions ?? []) {
      const cur = m.get(q.netuid) ?? { label: `SN${q.netuid}`, value: 0 };
      cur.value += q.tao_per_day ?? 0;
      m.set(q.netuid, cur);
    }
    const all = [...m.values()].filter((x) => x.value > 0).sort((a, b) => b.value - a.value);
    const top = all.slice(0, 7);
    const rest = all.slice(7).reduce((a, b) => a + b.value, 0);
    return rest > 0 ? [...top, { label: "Other", value: rest }] : top;
  })();

  return (
    <Chrome block={live.block} taoUsd={live.taoUsd} connected={live.connected}>
      <div className="mb-4">
        <div className="flex items-center gap-2">
          <h1 className="text-[15px] font-semibold tnum break-all">{coldkey}</h1>
          {mine && <Pill tone="accent">mine</Pill>}
        </div>
        <p className="text-[12.5px] mt-0.5" style={{ color: "var(--text-muted)" }}>Coldkey</p>
      </div>

      <div className="grid gap-3 mb-4" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(165px,1fr))" }}>
        <Stat label="Earning / day" value={money(d?.tao_per_day, 2)} />
        <Stat label="Per month" value={moneyCompact((d?.tao_per_day ?? 0) * 30, 1)} />
        <Stat label="Subnets" value={d?.subnets ?? "—"} />
        <Stat label="Hotkeys" value={d?.hotkeys ?? "—"} />
      </div>

      <div className="grid gap-4 mb-4" style={{ gridTemplateColumns: "minmax(0,1.4fr) minmax(0,1fr)" }}>
        <div className="card p-4">
          <h2 className="text-[13.5px] font-medium mb-3">Earnings by subnet</h2>
          {segs.length ? <StackBar segments={segs} /> :
            <span className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
              This coldkey holds UIDs but earns nothing right now.
            </span>}
        </div>
        <div className="card p-4">
          <h2 className="text-[13.5px] font-medium mb-3">Label</h2>
          <input className="input w-full" value={label} onChange={(e) => setLabel(e.target.value)}
                 placeholder="e.g. Rizzo validator, my miner…" />
          <label className="mt-3 flex items-center gap-2 text-[12.5px] cursor-pointer"
                 style={{ color: "var(--text-secondary)" }}>
            <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} />
            this coldkey is mine (adds it to Portfolio)
          </label>
          <div className="flex items-center gap-3 mt-3">
            <button className="btn btn-primary" onClick={save}>Save</button>
            {saved && <span className="text-[12px]" style={{ color: "var(--delta-up)" }}>saved ✓</span>}
          </div>
        </div>
      </div>

      <div className="card">
        <DataTable rows={d?.positions ?? []} cols={cols} initialSort="tao_per_day"
                   rowKey={(r) => `${r.netuid}-${r.uid}`} maxHeight={620}
                   tieBreak={(r) => r.netuid * 1000 + r.uid} />
      </div>
    </Chrome>
  );
}
