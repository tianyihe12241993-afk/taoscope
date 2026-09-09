"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { LineChart, StackBar } from "@/components/Charts";
import { Chrome } from "@/components/Chrome";
import { PageHeader } from "@/components/PageHeader";
import { Stat } from "@/components/Stat";
import { Col, DataTable } from "@/components/Table";
import { api, del, post } from "@/lib/api";
import { blockAge, compact, fmt, key, pct } from "@/lib/format";
import { useLive } from "@/lib/live";
import { Pill, useCurrency } from "@/lib/ui";

type Row = Record<string, any>;

export default function MyWork() {
  const live = useLive();
  const { money, moneyCompact } = useCurrency();
  const [cks, setCks] = useState<Row[]>([]);
  const [pf, setPf] = useState<Row | null>(null);
  const [hist, setHist] = useState<Row | null>(null);
  const [hours, setHours] = useState(72);
  const [addr, setAddr] = useState("");
  const [label, setLabel] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api("/api/me/coldkeys").then((d) => setCks(d.coldkeys)).catch(() => {});
    api("/api/me/portfolio").then(setPf).catch(() => {});
    api(`/api/me/history?hours=${hours}`).then(setHist).catch(() => {});
  }, [hours]);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const r = await post("/api/me/coldkeys", { coldkey: addr.trim(), label: label.trim() || null });
      if (r.registered_uids === 0) {
        setErr("Added — but this coldkey has no registered UIDs on any subnet right now.");
      }
      setAddr(""); setLabel("");
      load();
    } catch (e: any) {
      setErr(String(e?.message ?? "").includes("SS58")
        ? "That doesn't look like a valid SS58 coldkey address."
        : "Could not add that coldkey.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (ck: string) => {
    await del(`/api/me/coldkeys/${ck}`);
    load();
  };

  const t = pf?.totals;
  const head = live.block || 0;

  const cols: Col<Row>[] = [
    { key: "netuid", label: "SN", width: 56, align: "right", value: (r) => r.netuid, defaultDesc: false,
      render: (r) => <span className="tnum" style={{ color: "var(--text-muted)" }}>{r.netuid}</span> },
    { key: "subnet_name", label: "Subnet", value: (r) => r.subnet_name ?? "", defaultDesc: false,
      render: (r) => <Link href={`/subnet/${r.netuid}`} className="hover:underline font-medium">
        {r.subnet_name}</Link> },
    { key: "uid", label: "UID", align: "right", value: (r) => r.uid, defaultDesc: false,
      render: (r) => <span className="tnum">{r.uid}</span> },
    { key: "rank_in_subnet", label: "Rank", align: "right", value: (r) => r.rank_in_subnet,
      render: (r) => {
        const share = r.num_uids ? r.rank_in_subnet / r.num_uids : 1;
        const tone = share <= 0.15 ? "var(--delta-up)" : share >= 0.6 ? "var(--delta-down)" : "var(--text-primary)";
        return <span className="tnum" style={{ color: tone }}>#{r.rank_in_subnet}</span>;
      } },
    { key: "tao_per_day", label: "Earning / day", align: "right", value: (r) => r.tao_per_day ?? 0,
      render: (r) => <span className="tnum font-medium">{money(r.tao_per_day, 3)}</span> },
    { key: "emission_pct", label: "Emission %", align: "right", value: (r) => r.emission_pct ?? 0,
      render: (r) => <span className="tnum">{pct(r.emission_pct, 2)}</span> },
    { key: "incentive", label: "Incentive", align: "right", value: (r) => r.incentive ?? 0,
      render: (r) => <span className="tnum" style={{ color: (r.incentive ?? 0) > 0 ? "var(--text-primary)" : "var(--delta-down)" }}>
        {fmt(r.incentive, 4)}</span> },
    { key: "stake", label: "Stake", align: "right", value: (r) => r.stake ?? 0,
      render: (r) => <span className="tnum">{compact(r.stake, 1)}</span> },
    { key: "coldkey", label: "Coldkey", value: (r) => r.coldkey,
      render: (r) => <Link href={`/operators/${r.coldkey}`} className="hover:underline tnum text-[11.5px]"
                           style={{ color: "var(--accent)" }}>
        {r.coldkey_label || key(r.coldkey, 8, 5)}</Link> },
    { key: "validator_permit", label: "Role", width: 70, value: (r) => (r.validator_permit ? 1 : 0),
      render: (r) => r.validator_permit ? <Pill tone="accent">validator</Pill>
        : <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>miner</span> },
    { key: "block_at_registration", label: "Age", align: "right",
      value: (r) => r.block_at_registration ?? 0,
      render: (r) => {
        const days = head && r.block_at_registration
          ? ((head - r.block_at_registration) * 12) / 86400 : 0;
        const immuneDays = (r.immunity_period ?? 0) * 12 / 86400;
        const immune = days < immuneDays;
        return (
          <span className="tnum" style={{ color: immune ? "var(--delta-up)" : "var(--text-muted)" }}
                title={immune ? `Still immune from deregistration for ${(immuneDays - days).toFixed(1)} more days` : ""}>
            {blockAge(r.block_at_registration, head)}{immune ? " 🛡" : ""}
          </span>
        );
      } },
  ];

  return (
    <Chrome block={live.block} taoUsd={live.taoUsd} connected={live.connected}>
      <PageHeader
        title="My work"
        subtitle="What your hotkeys are actually earning. Register a coldkey and everything behind it shows up here."
      />

      <div className="grid gap-3 mb-4" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(165px,1fr))" }}>
        <Stat label="Earning / day" value={money(t?.tao_per_day, 2)}
              sub={t?.tao_per_day ? `${money((t.tao_per_day ?? 0) * 30, 1)} / month` : undefined} />
        <Stat label="Coldkeys" value={t?.coldkeys ?? 0} />
        <Stat label="Hotkeys" value={t?.hotkeys ?? 0} />
        <Stat label="Subnets" value={t?.subnets ?? 0} />
        <Stat label="Stake" value={compact(t?.stake, 1)} sub="alpha + tao" />
      </div>

      <div className="card p-4 mb-4">
        <div className="flex items-center justify-between mb-1">
          <div>
            <h2 className="text-[13.5px] font-medium">Recent work</h2>
            <p className="text-[11.5px] mt-0.5" style={{ color: "var(--text-muted)" }}>
              Earnings per day, valued at the subnet price recorded in each 15-minute bucket
              {hist?.change_pct !== null && hist?.change_pct !== undefined && (
                <span style={{ color: hist.change_pct >= 0 ? "var(--delta-up)" : "var(--delta-down)" }}>
                  {" · "}{hist.change_pct >= 0 ? "+" : ""}{hist.change_pct.toFixed(1)}% over the window
                </span>
              )}
            </p>
          </div>
          <div className="flex gap-1">
            {[24, 72, 168].map((h) => (
              <button key={h} onClick={() => setHours(h)}
                      className="px-2 py-0.5 rounded text-[11px]"
                      style={{
                        background: hours === h ? "var(--surface-2)" : "transparent",
                        color: hours === h ? "var(--text-primary)" : "var(--text-muted)",
                      }}>
                {h < 48 ? `${h}h` : `${h / 24}d`}
              </button>
            ))}
          </div>
        </div>
        {cks.length === 0 ? (
          <div className="flex items-center justify-center text-[12.5px] h-[210px]"
               style={{ color: "var(--text-muted)" }}>
            Register a coldkey below and your earnings curve builds from the next sweep.
          </div>
        ) : (
          <LineChart
            height={210}
            data={(hist?.points ?? []).map((p: Row) => ({ ts: p.ts, v: p.tao_per_day ?? 0 }))}
            valueFmt={(n) => money(n, 2)}
            label="mywork"
          />
        )}
      </div>

      <div className="grid gap-4 mb-4" style={{ gridTemplateColumns: "minmax(0,1fr) minmax(0,1.1fr)" }}>
        <div className="card p-4">
          <h2 className="text-[13.5px] font-medium mb-3">Registered coldkeys</h2>

          <form onSubmit={add} className="flex flex-col gap-2 mb-3">
            <input className="input" placeholder="5GsbTgfvgCH4… (SS58 coldkey)"
                   value={addr} onChange={(e) => setAddr(e.target.value)} required />
            <div className="flex gap-2">
              <input className="input flex-1" placeholder="label (optional)"
                     value={label} onChange={(e) => setLabel(e.target.value)} />
              <button className="btn btn-primary" disabled={busy || !addr.trim()}>
                {busy ? "Adding…" : "Add"}
              </button>
            </div>
            {err && <div className="text-[12px]" style={{ color: "var(--serious)" }}>{err}</div>}
          </form>

          {cks.length === 0 ? (
            <p className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
              None yet. Paste a coldkey above to track everything it runs.
            </p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {cks.map((k) => (
                <div key={k.coldkey} className="flex items-center gap-2 text-[12.5px] py-1.5 px-2 rounded-lg"
                     style={{ background: "var(--surface-2)" }}>
                  <Link href={`/operators/${k.coldkey}`} className="hover:underline tnum flex-1 truncate"
                        style={{ color: "var(--accent)" }}>
                    {k.label || `${k.coldkey.slice(0, 10)}…${k.coldkey.slice(-6)}`}
                  </Link>
                  <span className="tnum" style={{ color: "var(--text-secondary)" }}>
                    {k.hotkeys} hk · {k.subnets} sn
                  </span>
                  <span className="tnum font-medium">{money(k.tao_per_day, 2)}</span>
                  <button onClick={() => remove(k.coldkey)} title="Remove"
                          style={{ color: "var(--text-muted)" }}>×</button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card p-4">
          <h2 className="text-[13.5px] font-medium mb-1">Earnings by subnet</h2>
          <p className="text-[11.5px] mb-3" style={{ color: "var(--text-muted)" }}>
            Where your daily reward actually comes from
          </p>
          {pf?.by_subnet?.length ? (
            <StackBar segments={(() => {
              const top = pf.by_subnet.slice(0, 7).map((b: Row) => ({
                label: `SN${b.netuid}`, value: b.tao_per_day,
              }));
              const rest = pf.by_subnet.slice(7).reduce((a: number, b: Row) => a + b.tao_per_day, 0);
              return rest > 0 ? [...top, { label: "Other", value: rest }] : top;
            })()} />
          ) : (
            <p className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
              Nothing earning yet.
            </p>
          )}
        </div>
      </div>

      <div className="card">
        <div className="px-4 py-3 border-b flex items-center" style={{ borderColor: "var(--border)" }}>
          <h2 className="text-[13.5px] font-medium">All hotkeys</h2>
          <span className="ml-auto text-[12px]" style={{ color: "var(--text-muted)" }}>
            {pf?.positions?.length ?? 0} positions · 🛡 = still immune from deregistration
          </span>
        </div>
        <DataTable rows={pf?.positions ?? []} cols={cols} initialSort="tao_per_day"
                   rowKey={(r) => `${r.netuid}-${r.uid}`} maxHeight={640}
                   tieBreak={(r) => r.netuid * 1000 + r.uid} />
      </div>
    </Chrome>
  );
}
