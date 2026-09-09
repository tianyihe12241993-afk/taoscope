"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { LineChart, StackBar } from "@/components/Charts";
import { Chrome } from "@/components/Chrome";
import { Stat } from "@/components/Stat";
import { Col, DataTable } from "@/components/Table";
import { api, put } from "@/lib/api";
import { blockAge, compact, fmt, key, pct } from "@/lib/format";
import { Pill, Toggle, useCurrency } from "@/lib/ui";
import { useLive } from "@/lib/live";

type Row = Record<string, any>;
type Tab = "miners" | "coldkeys" | "info";
type Role = "all" | "miner" | "validator";

export default function SubnetPage() {
  const params = useParams();
  const netuid = Number(params.netuid);
  const live = useLive();
  const { money, moneyCompact, unit } = useCurrency();

  const [s, setS] = useState<Row | null>(null);
  const [hist, setHist] = useState<Row[]>([]);
  const [miners, setMiners] = useState<Row[]>([]);
  const [cks, setCks] = useState<Row[]>([]);
  const [tab, setTab] = useState<Tab>("miners");
  const [role, setRole] = useState<Role>("all");
  const [q, setQ] = useState("");
  const [hours, setHours] = useState(24);

  const load = useCallback(async () => {
    const [d, h, m, c] = await Promise.all([
      api(`/api/subnets/${netuid}`).catch(() => null),
      api(`/api/subnets/${netuid}/history?hours=${hours}`).catch(() => ({ points: [] })),
      api(`/api/subnets/${netuid}/miners?limit=512`).catch(() => ({ miners: [] })),
      api(`/api/subnets/${netuid}/coldkeys`).catch(() => ({ coldkeys: [] })),
    ]);
    if (d) setS(d);
    setHist(h.points ?? []);
    setMiners(m.miners ?? []);
    setCks(c.coldkeys ?? []);
  }, [netuid, hours]);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  const topEmission = Math.max(...miners.map((m) => m.emission_pct ?? 0), 0.0001);

  // Roles come from the API (see backend roles.py): a validator is a UID that
  // receives dividends; a coldkey is miner / validator / both by its hotkeys.
  const validatorUids = miners.filter((m) => m.role === "validator").length;
  const minerUids = miners.length - validatorUids;
  const ourUids = miners.filter((m) => m.is_ours).length;
  const term = q.trim().toLowerCase();
  const has = (v: unknown) => String(v ?? "").toLowerCase().includes(term);
  const matchUid = (m: Row) => !term || String(m.uid) === term
    || has(m.hotkey) || has(m.coldkey) || has(m.coldkey_label);
  const matchCk = (c: Row) => !term || has(c.coldkey) || has(c.label)
    || (c.uids ?? []).some((u: number) => String(u) === term);
  const shownUids = (role === "all" ? miners : miners.filter((m) => m.role === role)).filter(matchUid);
  const shownCks = (role === "all" ? cks : cks.filter((c) => c.role === role || c.role === "both")).filter(matchCk);
  const tint = (r: Row) => (r.is_ours ? "row-ours" : r.role === "validator" || r.role === "both" ? "row-validator" : undefined);

  const livePrice = live.subnets.find((x: Row) => x.netuid === netuid)?.price ?? s?.price;
  const head = live.block || s?.block || 0;

  const minerCols: Col<Row>[] = [
    { key: "uid", label: "UID", width: 60, align: "right", value: (r) => r.uid, defaultDesc: false,
      render: (r) => <span className="tnum" style={{ color: "var(--text-muted)" }}>{r.uid}</span> },
    { key: "rank_in_subnet", label: "#", width: 52, align: "right", value: (r) => r.rank_in_subnet, defaultDesc: false,
      render: (r) => <span className="tnum">{r.rank_in_subnet}</span> },
    { key: "emission_pct", label: "Emission %", align: "right", value: (r) => r.emission_pct ?? 0,
      render: (r) => (
        <div className="flex items-center justify-end gap-2">
          <div className="h-[6px] w-[46px] rounded-sm overflow-hidden"
               style={{ background: "rgba(127,127,127,0.12)" }}>
            <div style={{ width: `${Math.max(2, ((r.emission_pct ?? 0) / topEmission) * 100)}%`, height: "100%",
                          background: "var(--series-1)", borderTopRightRadius: 4, borderBottomRightRadius: 4 }} />
          </div>
          <span className="tnum w-[44px] text-right">{pct(r.emission_pct, 2)}</span>
        </div>
      ) },
    { key: "tao_per_day", label: "τ / day", align: "right", value: (r) => r.tao_per_day ?? 0,
      render: (r) => <span className="tnum">{money(r.tao_per_day, 3)}</span> },
    { key: "stake", label: "Stake", align: "right", value: (r) => r.stake ?? 0,
      render: (r) => <span className="tnum">{compact(r.stake, 1)}</span> },
    { key: "incentive", label: "Incentive", align: "right", value: (r) => r.incentive ?? 0,
      render: (r) => <span className="tnum">{fmt(r.incentive, 4)}</span> },
    { key: "dividends", label: "Dividends", align: "right", value: (r) => r.dividends ?? 0,
      render: (r) => <span className="tnum">{fmt(r.dividends, 4)}</span> },
    { key: "hotkey", label: "Hotkey", value: (r) => r.hotkey ?? "",
      render: (r) => (
        <span className="tnum" title={r.hotkey ?? ""} style={{ color: "var(--text-secondary)" }}>
          {key(r.hotkey)}
        </span>
      ) },
    { key: "coldkey", label: "Coldkey", value: (r) => r.coldkey ?? "",
      render: (r) => (
        <span className="inline-flex items-center gap-1.5">
          <Link href={`/operators/${r.coldkey}`} className="hover:underline tnum" title={r.coldkey ?? ""}
                style={{ color: r.is_ours ? "var(--delta-up)" : "var(--series-1)" }}>
            {r.coldkey_label || key(r.coldkey)}
          </Link>
          {r.is_ours && <Pill tone="good">mine</Pill>}
        </span>
      ) },
    { key: "role", label: "Role", width: 92,
      title: "validator = receives dividends; a permit alone does not count. 'vali+mine' is a validator that also earns incentive.",
      value: (r) => (r.role === "validator" ? (r.also_mines ? 2 : 1) : 0),
      render: (r) => r.role === "validator"
        ? <Pill tone="accent">{r.also_mines ? "vali+mine" : "vali"}</Pill>
        : <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>miner</span> },
    { key: "block_at_registration", label: "Age", align: "right",
      value: (r) => r.block_at_registration ?? 0,
      render: (r) => <span className="tnum" style={{ color: "var(--text-muted)" }}>
        {blockAge(r.block_at_registration, head)}
      </span> },
  ];

  const ckCols: Col<Row>[] = [
    { key: "coldkey", label: "Coldkey", value: (r) => r.coldkey ?? "",
      render: (r) => (
        <span className="inline-flex items-center gap-1.5">
          <Link href={`/operators/${r.coldkey}`} className="hover:underline tnum" title={r.coldkey ?? ""}
                style={{ color: r.is_ours ? "var(--delta-up)" : "var(--series-1)" }}>
            {r.label || key(r.coldkey, 10, 6)}
          </Link>
          {r.is_ours && <Pill tone="good">mine</Pill>}
        </span>
      ) },
    { key: "hotkeys", label: "Hotkeys", align: "right", value: (r) => r.hotkeys,
      title: "Hotkeys on this subnet; split shown when the operator runs both roles",
      render: (r) => (
        <span className="tnum">
          {r.hotkeys}
          {r.role === "both" && (
            <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>
              {" "}({r.miner_hotkeys}m · {r.validator_hotkeys}v)
            </span>
          )}
        </span>
      ) },
    { key: "emission_pct", label: "Emission %", align: "right", value: (r) => r.emission_pct ?? 0,
      render: (r) => <span className="tnum">{pct(r.emission_pct, 2)}</span> },
    { key: "tao_per_day", label: "τ / day", align: "right", value: (r) => r.tao_per_day ?? 0,
      render: (r) => (
        <span className="tnum"
              title={r.role === "both"
                ? `mining ${money(r.miner_tao_per_day, 2)} · validating ${money(r.validator_tao_per_day, 2)}`
                : undefined}>
          {money(r.tao_per_day, 2)}
        </span>
      ) },
    { key: "stake", label: "Stake", align: "right", value: (r) => r.stake ?? 0,
      render: (r) => <span className="tnum">{compact(r.stake, 1)}</span> },
    { key: "role", label: "Role", width: 92,
      title: "By the roles of the operator's hotkeys here: validator = every hotkey receives dividends, both = a mix",
      value: (r) => (r.role === "validator" ? 2 : r.role === "both" ? 1 : 0),
      render: (r) => r.role === "validator" ? <Pill tone="accent">validator</Pill>
        : r.role === "both" ? <Pill tone="warn">both</Pill>
        : <span className="text-[11px]" style={{ color: "var(--text-muted)" }}>miner</span> },
    { key: "uids", label: "UIDs",
      render: (r) => <span className="tnum" style={{ color: "var(--text-muted)" }}>
        {(r.uids ?? []).slice(0, 8).join(", ")}{(r.uids ?? []).length > 8 ? "…" : ""}
      </span> },
  ];

  const segs = (() => {
    const top = cks.slice(0, 7).map((c) => ({ label: key(c.coldkey, 6, 4), value: c.emission ?? 0 }));
    const rest = cks.slice(7).reduce((a, c) => a + (c.emission ?? 0), 0);
    return rest > 0 ? [...top, { label: "Other", value: rest }] : top;
  })();

  return (
    <Chrome block={live.block} taoUsd={live.taoUsd} connected={live.connected}>
      <div className="flex items-start gap-4 mb-4">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="text-[11px] px-1.5 py-0.5 rounded tnum"
                  style={{ background: "rgba(127,127,127,0.14)", color: "var(--text-secondary)" }}>
              SN{netuid}
            </span>
            <h1 className="text-[20px] font-semibold tracking-tight">
              {s?.name ?? "…"} <span style={{ color: "var(--text-muted)" }}>{s?.symbol}</span>
            </h1>
          </div>
          {s?.chain_description && (
            <p className="mt-1 text-[13px] max-w-[820px]" style={{ color: "var(--text-secondary)" }}>
              {s.chain_description}
            </p>
          )}
          <div className="mt-1.5 flex gap-3 text-[12px]">
            {s?.github_repo && <a className="hover:underline" style={{ color: "var(--series-1)" }}
                                  href={s.github_repo} target="_blank" rel="noreferrer">github</a>}
            {s?.website && <a className="hover:underline" style={{ color: "var(--series-1)" }}
                              href={s.website} target="_blank" rel="noreferrer">website</a>}
            {s?.dashboard_url && <a className="hover:underline" style={{ color: "var(--series-3)" }}
                                    href={s.dashboard_url} target="_blank" rel="noreferrer">dashboard</a>}
            {s?.docs_url && <a className="hover:underline" style={{ color: "var(--series-1)" }}
                               href={s.docs_url} target="_blank" rel="noreferrer">docs</a>}
            {s?.discord && <a className="hover:underline" style={{ color: "var(--series-7)" }}
                              href={s.discord} target="_blank" rel="noreferrer">discord</a>}
          </div>
        </div>
      </div>

      <div className="grid gap-3 mb-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px,1fr))" }}>
        <Stat label="Alpha price"
              value={unit === "USD" && live.taoUsd
                ? `$${((livePrice ?? 0) * live.taoUsd).toFixed(4)}`
                : `τ${(livePrice ?? 0).toFixed(6)}`}
              sub={unit === "USD" ? `τ${(livePrice ?? 0).toFixed(6)}`
                : (live.taoUsd ? `$${((livePrice ?? 0) * live.taoUsd).toFixed(4)}` : undefined)} />
        <Stat label="Market cap" value={moneyCompact(s?.market_cap_tao, 1)} />
        <Stat label="Miners / day"
              value={moneyCompact(s?.miner_tao_per_day, 1)}
              sub={`validators ${moneyCompact(s?.validator_tao_per_day, 1)} · owner ${moneyCompact(s?.owner_tao_per_day, 1)}`}
              hint="Every subnet emits the same alpha per day: ~18% owner cut, then a 50/50 split between the miner and validator pools. What differs between subnets is how many UIDs share the miner half." />
        <Stat label="Emission share" value={pct((s?.emission_share ?? 0) * 100, 2)}
              sub={`≈ ${moneyCompact((s?.realized_tao_per_hour ?? 0) * 24, 1)}/day to UIDs`} />
        <Stat label="Reg cost" value={money(s?.burn_tao, 3)}
              sub={s?.registration_allowed ? "open" : "closed"} />
        <Stat label="UIDs" value={`${s?.num_uids ?? "—"}/${s?.max_uids ?? "—"}`}
              sub={`${miners.length ? validatorUids : (s?.validator_count ?? "—")} validators · ${s?.active_uids ?? "—"} setting weights`}
              hint="Validators are UIDs receiving dividends (a permit alone does not count). 'Setting weights' counts neurons that set weights within activity_cutoff — weight-setting, not whether a miner is running." />
        <Stat label="Operators" value={s?.unique_coldkeys ?? "—"} sub="distinct coldkeys" />
        <Stat label="Top coldkey" value={pct(s?.top_coldkey_pct, 1)}
              sub={`${s?.top_coldkey_hotkeys ?? 0} hotkeys`} />
      </div>

      <div className="grid gap-3 mb-4" style={{ gridTemplateColumns: "minmax(0,2fr) minmax(0,1fr)" }}>
        <div className="card p-3">
          <div className="flex items-center justify-between mb-1">
            <h2 className="text-[13px] font-medium">Alpha price (τ)</h2>
            <div className="flex gap-1">
              {[6, 24, 168].map((h) => (
                <button key={h} onClick={() => setHours(h)}
                        className="px-2 py-0.5 rounded text-[11px]"
                        style={{
                          background: hours === h ? "rgba(127,127,127,0.16)" : "transparent",
                          color: hours === h ? "var(--text-primary)" : "var(--text-muted)",
                        }}>
                  {h < 24 ? `${h}h` : `${h / 24}d`}
                </button>
              ))}
            </div>
          </div>
          <LineChart
            data={hist.map((p) => ({ ts: p.ts, v: p.price }))}
            valueFmt={(n) => n.toFixed(6)}
            label={`sn${netuid}`}
          />
        </div>

        <div className="card p-3">
          <h2 className="text-[13px] font-medium mb-1">Emission by coldkey</h2>
          <p className="text-[11.5px] mb-3" style={{ color: "var(--text-muted)" }}>
            Who actually earns here — {s?.unique_coldkeys ?? "—"} operators across {s?.num_uids ?? "—"} UIDs
          </p>
          {segs.length > 0 ? <StackBar segments={segs} /> :
            <div className="text-[12px]" style={{ color: "var(--text-muted)" }}>waiting for sweep…</div>}
        </div>
      </div>

      <div className="card">
        <div className="flex items-center gap-1 px-3 py-2 border-b" style={{ borderColor: "var(--border)" }}>
          {(["miners", "coldkeys", "info"] as Tab[]).map((t) => (
            <button key={t} onClick={() => setTab(t)}
                    className="px-3 py-1.5 rounded-md text-[12.5px] capitalize"
                    style={{
                      background: tab === t ? "rgba(127,127,127,0.14)" : "transparent",
                      color: tab === t ? "var(--text-primary)" : "var(--text-secondary)",
                    }}>
              {t === "info" ? "Info & links" : t === "miners" ? "UIDs" : t}
            </button>
          ))}
          {tab !== "info" && (
            <>
              <span className="ml-3" title="Validators receive dividends; everything else is a miner">
                <Toggle value={role} onChange={(v) => setRole(v as Role)}
                        options={[{ value: "all", label: "All" }, { value: "miner", label: "Miners" },
                                  { value: "validator", label: "Validators" }]} />
              </span>
              <input value={q} onChange={(e) => setQ(e.target.value)} className="input w-[250px] ml-1"
                     placeholder={tab === "miners" ? "uid, hotkey, coldkey or label…" : "coldkey, label or uid…"} />
              <span className="ml-2 flex items-center gap-3 text-[11.5px]" style={{ color: "var(--text-muted)" }}>
                <span><span className="swatch mr-1" style={{ background: "color-mix(in srgb, var(--good) 45%, transparent)" }} />mine</span>
                <span><span className="swatch mr-1" style={{ background: "color-mix(in srgb, var(--accent-bright) 35%, transparent)" }} />validator</span>
                <span><span className="swatch mr-1" style={{ background: "var(--surface-2)", border: "1px solid var(--border-strong)" }} />miner</span>
              </span>
            </>
          )}
          <span className="ml-auto text-[12px]" style={{ color: "var(--text-muted)" }}>
            {tab === "miners"
              ? `${shownUids.length} of ${miners.length} UIDs · ${minerUids} miners · ${validatorUids} validators · ${ourUids} mine`
              : tab === "coldkeys"
                ? `${shownCks.length} of ${cks.length} operators · ${cks.filter((c) => c.role === "both").length} run both roles`
                : ""}
          </span>
        </div>

        {tab === "miners" && (
          <DataTable rows={shownUids} cols={minerCols} initialSort="emission_pct" rowClass={tint}
                     rowKey={(r) => r.uid} maxHeight={620} />
        )}
        {tab === "coldkeys" && (
          <DataTable rows={shownCks} cols={ckCols} initialSort="emission_pct" rowClass={tint}
                     rowKey={(r) => r.coldkey} maxHeight={620} />
        )}
        {tab === "info" && <MetaEditor netuid={netuid} onSaved={load} chain={s} />}
      </div>
    </Chrome>
  );
}

/* ---------- requirement 3: your own links and notes ---------- */
const FIELDS: { k: string; label: string; ph: string }[] = [
  { k: "dashboard_url", label: "Dashboard URL", ph: "https://…" },
  { k: "github_repo", label: "GitHub repo", ph: "overrides the on-chain value" },
  { k: "docs_url", label: "Docs", ph: "https://…" },
  { k: "website", label: "Website", ph: "overrides the on-chain value" },
  { k: "discord", label: "Discord", ph: "https://discord.gg/…" },
  { k: "twitter", label: "X / Twitter", ph: "https://x.com/…" },
];

function MetaEditor({ netuid, onSaved, chain }: { netuid: number; onSaved: () => void; chain: Row | null }) {
  const [meta, setMeta] = useState<Row>({});
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api(`/api/subnets/${netuid}/meta`).then(setMeta).catch(() => setMeta({}));
  }, [netuid]);

  const save = async () => {
    setBusy(true);
    const body: Row = { notes: meta.notes ?? "", watch: !!meta.watch };
    FIELDS.forEach((f) => (body[f.k] = meta[f.k] ?? ""));
    body.tags = typeof meta.tags === "string"
      ? meta.tags.split(",").map((t: string) => t.trim()).filter(Boolean)
      : meta.tags ?? [];
    try {
      await put(`/api/subnets/${netuid}/meta`, body);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      onSaved();
    } finally {
      setBusy(false);
    }
  };

  const set = (k: string, v: any) => setMeta((m) => ({ ...m, [k]: v }));
  const inputStyle = {
    background: "var(--plane)", border: "1px solid var(--border)", color: "var(--text-primary)",
  };

  return (
    <div className="p-4 max-w-[900px]">
      <p className="text-[12px] mb-4" style={{ color: "var(--text-muted)" }}>
        Anything you enter here overrides what the subnet publishes on-chain. Leave a field
        blank to keep the chain value.
      </p>

      <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(280px,1fr))" }}>
        {FIELDS.map((f) => (
          <div key={f.k}>
            <label className="text-[11.5px]" style={{ color: "var(--text-secondary)" }}>{f.label}</label>
            <input
              value={meta[f.k] ?? ""} placeholder={f.ph}
              onChange={(e) => set(f.k, e.target.value)}
              className="mt-1 w-full px-2.5 py-1.5 rounded-md text-[12.5px] outline-none"
              style={inputStyle}
            />
            {chain?.[`chain_${f.k === "github_repo" ? "github" : f.k === "website" ? "url" : f.k}`] && (
              <div className="mt-1 text-[11px] truncate" style={{ color: "var(--text-muted)" }}>
                on-chain: {chain[`chain_${f.k === "github_repo" ? "github" : f.k === "website" ? "url" : f.k}`]}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="mt-3">
        <label className="text-[11.5px]" style={{ color: "var(--text-secondary)" }}>
          Tags (comma separated)
        </label>
        <input
          value={Array.isArray(meta.tags) ? meta.tags.join(", ") : meta.tags ?? ""}
          onChange={(e) => set("tags", e.target.value)}
          placeholder="llm, inference, watching"
          className="mt-1 w-full px-2.5 py-1.5 rounded-md text-[12.5px] outline-none"
          style={inputStyle}
        />
      </div>

      <div className="mt-3">
        <label className="text-[11.5px]" style={{ color: "var(--text-secondary)" }}>Private notes</label>
        <textarea
          value={meta.notes ?? ""} onChange={(e) => set("notes", e.target.value)} rows={5}
          placeholder="Scoring mechanics, what the king does, submission traps…"
          className="mt-1 w-full px-2.5 py-2 rounded-md text-[12.5px] outline-none resize-y"
          style={inputStyle}
        />
      </div>

      <div className="mt-3 flex items-center gap-4">
        <label className="flex items-center gap-2 text-[12.5px] cursor-pointer"
               style={{ color: "var(--text-secondary)" }}>
          <input type="checkbox" checked={!!meta.watch} onChange={(e) => set("watch", e.target.checked)} />
          watch this subnet
        </label>
        <button onClick={save} disabled={busy}
                className="px-3.5 py-1.5 rounded-md text-[12.5px] font-medium disabled:opacity-60"
                style={{ background: "var(--series-1)", color: "#fff" }}>
          {busy ? "Saving…" : "Save"}
        </button>
        {saved && <span className="text-[12px]" style={{ color: "var(--good)" }}>saved ✓</span>}
      </div>
    </div>
  );
}
