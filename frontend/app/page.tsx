"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ActivityFeed } from "@/components/ActivityFeed";
import { Avatar } from "@/components/Avatar";
import { Chrome } from "@/components/Chrome";
import { ColumnPicker } from "@/components/ColumnPicker";
import { PageHeader } from "@/components/PageHeader";
import { Stat } from "@/components/Stat";
import { Col, DataTable } from "@/components/Table";
import { Tooltip } from "@/components/Tooltip";
import { api, del, post } from "@/lib/api";
import { useColumnPrefs } from "@/lib/columns";
import { compact, countdown, fmt, key as shortKey, pct, stamp } from "@/lib/format";
import { useLive } from "@/lib/live";
import { Pill, Toggle, useCurrency } from "@/lib/ui";

type Row = Record<string, any>;

interface Criteria {
  taoDayMin: number; operatorsMax: number; topCkMax: number; regCostMax: number;
  earningMin: number; paybackMax: number; ageMax: number; freeUidsMin: number;
  openOnly: boolean; mineOnly: boolean; watchedOnly: boolean; prelaunchOnly: boolean; q: string;
}

const BLANK: Criteria = {
  taoDayMin: 0, operatorsMax: 300, topCkMax: 100, regCostMax: 100,
  earningMin: 0, paybackMax: 3650, ageMax: 4000, freeUidsMin: 0,
  openOnly: false, mineOnly: false, watchedOnly: false, prelaunchOnly: false, q: "",
};

const PRESETS: { name: string; hint: string; c: Partial<Criteria> }[] = [
  { name: "Good for miners", hint: "real reward, a genuine chance of earning, entry you can recover",
    c: { taoDayMin: 10, earningMin: 10, topCkMax: 60, openOnly: true, paybackMax: 30 } },
  { name: "Uncontested", hint: "few operators chasing meaningful emission",
    c: { taoDayMin: 8, operatorsMax: 60, openOnly: true } },
  { name: "New subnets", hint: "started emitting in the last 60 days", c: { ageMax: 60 } },
  { name: "Pre-launch", hint: "registered but not yet emitting — get in before rewards start",
    c: { prelaunchOnly: true, taoDayMin: 0 } },
  { name: "Cheap entry", hint: "registration under τ0.05 and open",
    c: { regCostMax: 0.05, openOnly: true } },
  { name: "Room to join", hint: "free UIDs, nobody gets deregistered to make space",
    c: { freeUidsMin: 1, openOnly: true } },
  { name: "Where I mine", hint: "subnets one of my coldkeys is registered on",
    c: { mineOnly: true } },
];

/** Every column the table can show, in its default order. The user reorders and
 *  hides these from the Columns picker; the choice persists per browser. */
const COLUMN_ORDER = [
  "netuid", "name", "reg", "submission", "market_cap_tao", "emission_share", "miner_tao_per_day",
  "burn", "best_miner_tao_per_day", "unique_coldkeys", "reward_per_operator", "pct_miners_earning",
  "payback_days", "uids", "links",
  // off by default
  "price", "earning_miners", "validator_count", "top_coldkey_pct", "age_days", "uids_free",
  "validator_tao_per_day", "owner_tao_per_day", "tempo", "immunity",
];
const HIDDEN_BY_DEFAULT = [
  "price", "earning_miners", "validator_count", "top_coldkey_pct", "age_days", "uids_free",
  "validator_tao_per_day", "owner_tao_per_day", "tempo", "immunity",
];

/** Sort key for the submission column: soonest deadline first, then eval, rolling, closed; no tracker sinks. */
function submissionSort(w: Row | null | undefined): number | null {
  if (!w) return null;
  const secs = (iso?: string | null) => (iso ? Math.max(0, (new Date(iso).getTime() - Date.now()) / 1000) : 1e8);
  switch (w.state) {
    case "open": return secs(w.closes_at);
    case "eval": return 2e8 + secs(w.ends_at);
    case "rolling": return 4e8;
    case "closed": return 5e8 + secs(w.opens_at);
    default: return 6e8;
  }
}

function GitHubMark({ size = 12 }: { size?: number }) {
  return (
    <svg viewBox="0 0 16 16" width={size} height={size} fill="currentColor" aria-hidden className="shrink-0">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}

function SubmissionCell({ w }: { w: Row | null | undefined }) {
  if (!w || w.state === "unknown") {
    return <span style={{ color: "var(--text-muted)" }}
                 title={w ? "Tracker has no usable window for this subnet right now" : "No competition tracker for this subnet"}>—</span>;
  }
  const tone = w.state === "open" ? "good" : w.state === "eval" ? "warn" : "neutral";
  const text = w.state === "open" ? (w.closes_at ? `closes in ${countdown(w.closes_at)}` : "no deadline")
    : w.state === "eval" ? (w.ends_at ? `ends in ${countdown(w.ends_at)}` : "scoring")
    : w.state === "closed" ? (w.opens_at ? `opens in ${countdown(w.opens_at)}` : "closed")
    : "any time";
  const lines = [
    w.label, w.note,
    w.opens_at && `opens ${stamp(w.opens_at)}`,
    w.closes_at && `closes ${stamp(w.closes_at)}`,
    w.ends_at && `ends ${stamp(w.ends_at)}`,
    w.polled_at && `tracker polled ${stamp(w.polled_at)}`,
  ].filter(Boolean);
  return (
    <span className="inline-flex items-center gap-1.5 min-w-0" title={lines.join("\n")}>
      <Pill tone={tone}>{w.state}</Pill>
      <span className="tnum truncate text-[12px]">{text}</span>
    </span>
  );
}
const LOCKED_COLUMNS = ["netuid", "name"];

/** How many criteria differ from the blank state — drives the "Filters (n)" badge. */
function activeCount(c: Criteria): number {
  return (Object.keys(BLANK) as (keyof Criteria)[])
    .filter((k) => k !== "q" && c[k] !== BLANK[k]).length + (c.q.trim() ? 1 : 0);
}

export default function Overview() {
  const live = useLive();
  const { money, moneyCompact, unit, toggle: toggleUnit } = useCurrency();
  const [rows, setRows] = useState<Row[]>([]);
  const [net, setNet] = useState<Row | null>(null);
  const [c, setC] = useState<Criteria>(BLANK);
  const [showFilters, setShowFilters] = useState(false);
  const [saved, setSaved] = useState<Row[]>([]);
  const [activePreset, setActivePreset] = useState<string | null>(null);
  const { prefs, toggle: toggleCol, move: moveCol, reorder: reorderCol, reset: resetCols } =
    useColumnPrefs("taoscope-subnet-columns", COLUMN_ORDER, HIDDEN_BY_DEFAULT);

  const load = useCallback(async () => {
    const [s, n, f] = await Promise.all([
      api("/api/screen").catch(() => null),
      api("/api/network").catch(() => null),
      api("/api/filters").catch(() => null),
    ]);
    if (s) setRows(s.subnets);
    if (n) setNet(n);
    if (f) setSaved(f.filters);
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  // fast price ticks patch the slower screener payload
  const merged = useMemo(() => {
    if (!live.subnets.length) return rows;
    const byId = new Map(live.subnets.map((s: Row) => [s.netuid, s]));
    return rows.map((r) => {
      const l = byId.get(r.netuid);
      if (!l) return r;
      // re-price the miner/validator figures against the live price so they stay
      // consistent between the 60s screener refreshes
      const ratio = r.price ? (l.price ?? r.price) / r.price : 1;
      return { ...r, price: l.price, emission_share: l.emission_share,
               market_cap_tao: l.market_cap_tao,
               tao_per_day: (l.realized_tao_per_hour ?? 0) * 24,
               miner_tao_per_day: (r.miner_tao_per_day ?? 0) * ratio,
               validator_tao_per_day: (r.validator_tao_per_day ?? 0) * ratio,
               reward_per_operator: (r.reward_per_operator ?? 0) * ratio };
    });
  }, [rows, live.subnets]);

  const maxShare = useMemo(
    () => Math.max(...merged.map((r) => r.emission_share ?? 0), 0.0001), [merged]);

  // rank by emission share across every subnet, so #1 stays #1 under any filter
  const emissionRank = useMemo(() => {
    const order = [...merged].sort((a, b) =>
      ((b.emission_share ?? 0) - (a.emission_share ?? 0)) || (a.netuid - b.netuid));
    return new Map<number, number>(order.map((r, i) => [r.netuid, i + 1]));
  }, [merged]);

  const set = <K extends keyof Criteria>(k: K, v: Criteria[K]) => {
    setC((p) => ({ ...p, [k]: v }));
    setActivePreset(null);
  };

  const applyPreset = (p: (typeof PRESETS)[number]) => {
    setC({ ...BLANK, ...p.c });
    setActivePreset(p.name);
    setShowFilters(true);
  };

  const out = useMemo(() => {
    const term = c.q.trim().toLowerCase();
    return merged.filter((r) => {
      if ((r.miner_tao_per_day ?? 0) < c.taoDayMin) return false;
      if ((r.unique_coldkeys ?? 0) > c.operatorsMax) return false;
      if ((r.top_coldkey_pct ?? 0) > c.topCkMax) return false;
      if ((r.burn_tao ?? 0) > c.regCostMax) return false;
      if ((r.pct_miners_earning ?? 0) < c.earningMin) return false;
      if (c.paybackMax < 3650 && (r.payback_days === null || r.payback_days > c.paybackMax)) return false;
      if ((r.age_days ?? 99999) > c.ageMax) return false;
      if ((r.uids_free ?? 0) < c.freeUidsMin) return false;
      if (c.openOnly && !r.registration_allowed) return false;
      if (c.mineOnly && !r.i_am_in) return false;
      if (c.watchedOnly && !r.watch) return false;
      if (c.prelaunchOnly && r.is_active !== false) return false;
      if (term && !(String(r.netuid) === term ||
                    (r.name ?? "").toLowerCase().includes(term) ||
                    (r.chain_description ?? "").toLowerCase().includes(term))) return false;
      return true;
    });
  }, [merged, c]);

  const saveCurrent = async () => {
    const name = prompt("Name this filter:");
    if (!name) return;
    await post("/api/filters", { name, criteria: c });
    load();
  };

  const taoUsd = live.taoUsd || net?.tao_usd || 0;
  const n = activeCount(c);

  const allCols: Col<Row>[] = [
    { key: "netuid", label: "SN", width: 54, align: "right", value: (r) => r.netuid, defaultDesc: false,
      render: (r) => <span className="tnum" style={{ color: "var(--text-muted)" }}>{r.netuid}</span> },
    { key: "name", label: "Subnet", width: 184, value: (r) => r.name ?? "", defaultDesc: false,
      title: "Hover a name for the subnet's description",
      render: (r) => (
        <span className="flex items-center gap-2">
          <Avatar src={r.chain_logo} name={r.name} seed={r.netuid} />
          <Tooltip text={(r.chain_description ?? "").trim() || "No description published on-chain."}>
            <Link href={`/subnet/${r.netuid}`} className="font-medium hover:underline truncate">
              {r.name || `subnet-${r.netuid}`}
            </Link>
          </Tooltip>
          {r.i_am_in && <Pill tone="accent">mine</Pill>}
          {r.is_active === false && <Pill tone="warn">pre-launch</Pill>}
          {r.is_active !== false && r.age_days < 60 && <Pill tone="good">new</Pill>}
        </span>
      ) },
    { key: "reg", label: "Reg", align: "right", width: 104,
      title: "Cost to register one UID right now (the burn), in the active currency. 'closed' means registration is switched off.",
      value: (r) => r.burn_tao ?? 0,
      render: (r) => (
        <span className="inline-flex items-center justify-end gap-1.5">
          <span className="tnum">{money(r.burn_tao, 3)}</span>
          {!r.registration_allowed && (
            <span className="text-[10.5px] font-medium" style={{ color: "var(--delta-down)" }}>closed</span>
          )}
        </span>
      ) },
    { key: "submission", label: "Submission", width: 176, defaultDesc: false,
      title: "Current submission window from the competition tracker: open = you can submit, closes in…; eval = submissions closed while scoring runs; rolling = submit any time; closed = nothing open. Hover for the round and exact times. Only subnets with a tracker adapter show one.",
      value: (r) => submissionSort(r.submission),
      render: (r) => <SubmissionCell w={r.submission} /> },
    { key: "market_cap_tao", label: "Market cap", align: "right", width: 104,
      value: (r) => r.market_cap_tao ?? 0,
      render: (r) => <span className="tnum">{moneyCompact(r.market_cap_tao, 1)}</span> },
    { key: "emission_share", label: "Emission", align: "right", width: 160,
      title: "Share of network emission, by moving price. #n is the subnet's rank by that share across all subnets.",
      value: (r) => r.emission_share ?? 0,
      render: (r) => (
        <span className="inline-flex items-center justify-end gap-2">
          <span className="tnum text-[11px] w-[32px] text-right" style={{ color: "var(--text-muted)" }}>
            #{emissionRank.get(r.netuid) ?? "—"}
          </span>
          <span className="h-[5px] w-[42px] rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
            <span className="block h-full rounded-full"
                  style={{ width: `${Math.max(3, ((r.emission_share ?? 0) / maxShare) * 100)}%`,
                           background: "var(--accent-bright)" }} />
          </span>
          <span className="tnum w-[42px] text-right">{pct((r.emission_share ?? 0) * 100, 2)}</span>
        </span>
      ) },
    { key: "miner_tao_per_day", label: "Miners / day", align: "right", width: 108,
      title: "The miner pot: TAO paid to miners per day. Every subnet emits the same alpha — ~18% owner cut, then the rest splits 50/50 between miners and validators. What varies hugely is how many UIDs share the miner half: sometimes hundreds, sometimes one.",
      value: (r) => r.miner_tao_per_day ?? 0,
      render: (r) => {
        const v = r.miner_tao_per_day ?? 0;
        return (
          <span className="tnum font-medium"
                title={`shared by ${r.earning_miners ?? 0} earning UID(s) · validators ${moneyCompact(r.validator_tao_per_day, 1)}/day · owner ${moneyCompact(r.owner_tao_per_day, 1)}/day`}>
            {moneyCompact(v, 1)}
          </span>
        );
      } },
    { key: "burn", label: "Burn", align: "right", width: 104,
      title: "Share of miner emission the chain withheld from miners last tempo (SubtensorModule::MinerBurned). Incentive routed to the subnet owner's own hotkeys is burned or recycled, never paid — at 100% the 'Miners / day' pot reaches no miner at all. '—' means the subnet paid miners nothing last tempo, so there was nothing to burn.",
      // null, not -1: Table sinks missing values in BOTH sort directions, so a
      // subnet with no miner emission never tops an ascending "burns least" sort.
      value: (r) => ((r.miner_tao_per_day ?? 0) > 0 && r.miner_burned != null) ? r.miner_burned * 100 : null,
      render: (r) => {
        const burned = r.miner_burned;
        if (burned === null || burned === undefined || (r.miner_tao_per_day ?? 0) <= 0) {
          return (
            <span style={{ color: "var(--text-muted)" }}
                  title="No miner emission last tempo — nothing to burn.">—</span>
          );
        }
        const v = burned * 100;
        const share = r.owner_incentive_share;
        // The chain value is per tempo; the reconstruction is from the latest
        // metagraph sweep. Where they disagree, show both rather than let the
        // chain's number stand alone looking confident.
        const disagree = share !== null && share !== undefined && Math.abs(share - burned) > 0.05;
        const tone = v >= 50 ? "var(--delta-down)" : v >= 10 ? "var(--serious)" : "var(--delta-up)";
        const tip = disagree
          ? `Chain withheld ${v.toFixed(1)}% last tempo, but the owner's keys hold ${(share * 100).toFixed(1)}% of incentive in the latest sweep. They disagree here — read both.`
          : `${v.toFixed(1)}% of last tempo's miner emission went to the owner's hotkeys and was burned or recycled` +
            (share !== null && share !== undefined ? ` · owner keys hold ${(share * 100).toFixed(1)}% of incentive` : "");
        return (
          <span className="inline-flex items-center justify-end gap-2" title={tip}>
            <span className="h-[5px] w-[30px] rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
              <span className="block h-full rounded-full"
                    style={{ width: `${Math.max(v > 0 ? 3 : 0, Math.min(100, v))}%`, background: tone }} />
            </span>
            <span className="tnum w-[44px] text-right" style={{ color: tone }}>
              {v >= 99.95 ? "100%" : `${v.toFixed(1)}%`}
            </span>
            <span className="tnum text-[11px] w-[8px]" style={{ color: "var(--warning)" }} aria-hidden>
              {disagree ? "≠" : ""}
            </span>
          </span>
        );
      } },
    { key: "best_miner_tao_per_day", label: "Top miner", align: "right", width: 100,
      title: "The best-earning miner hotkey's own emission per day at today's price. Validator hotkeys (those receiving dividends) are left out entirely, so no validator emission is in this number.",
      value: (r) => r.best_miner_tao_per_day ?? 0,
      render: (r) => (
        <span className="tnum" style={{ color: "var(--text-secondary)" }}
              title={r.top_miner_uid === null || r.top_miner_uid === undefined
                ? undefined : `uid ${r.top_miner_uid} · ${shortKey(r.top_miner_hotkey)}`}>
          {moneyCompact(r.best_miner_tao_per_day, 2)}
        </span>
      ) },
    { key: "unique_coldkeys", label: "Rivals", align: "right", width: 74,
      title: "Distinct coldkeys competing here",
      value: (r) => r.unique_coldkeys ?? 0,
      render: (r) => <span className="tnum">{r.unique_coldkeys ?? "—"}</span> },
    { key: "reward_per_operator", label: "Per rival", align: "right", width: 88,
      title: "MINER reward per day divided by the number of competing operators",
      value: (r) => r.reward_per_operator ?? 0,
      render: (r) => <span className="tnum">{money(r.reward_per_operator, 3)}</span> },
    { key: "pct_miners_earning", label: "Miners earning", align: "right", width: 178,
      title: "Share of miner UIDs with any incentive at all, then the count: earning miners / all miner UIDs. Validators (permit holders receiving dividends) are left out. On most subnets this is under 25% — a big prize means nothing if you land in the zero-earning majority.",
      value: (r) => r.pct_miners_earning ?? -1,
      render: (r) => {
        const v = r.pct_miners_earning;
        if (v === null || v === undefined) return <span style={{ color: "var(--text-muted)" }}>—</span>;
        const tone = v >= 25 ? "var(--delta-up)" : v >= 8 ? "var(--serious)" : "var(--delta-down)";
        return (
          <span className="inline-flex items-center justify-end gap-2">
            <span className="h-[5px] w-[34px] rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
              <span className="block h-full rounded-full"
                    style={{ width: `${Math.max(3, Math.min(100, v * 2))}%`, background: tone }} />
            </span>
            <span className="tnum w-[40px] text-right" style={{ color: tone }}>{v.toFixed(1)}%</span>
            <span className="tnum text-[11px] w-[54px] text-right" style={{ color: "var(--text-muted)" }}>
              {r.earning_miners ?? 0}/{r.miners ?? 0}
            </span>
          </span>
        );
      } },
    { key: "payback_days", label: "Payback", align: "right", width: 82,
      title: "Days for a median earning miner to recover the registration cost",
      value: (r) => r.payback_days ?? null,
      render: (r) => {
        if (r.payback_days === null || r.payback_days === undefined)
          return <span style={{ color: "var(--text-muted)" }}>—</span>;
        const d = r.payback_days;
        const tone = d <= 7 ? "var(--delta-up)" : d <= 60 ? "var(--text-primary)" : "var(--delta-down)";
        return <span className="tnum" style={{ color: tone }}>{d < 1 ? "<1d" : `${d.toFixed(0)}d`}</span>;
      } },
    { key: "uids", label: "UIDs", align: "right", width: 88, value: (r) => r.num_uids ?? 0,
      render: (r) => <span className="tnum" style={{ color: "var(--text-secondary)" }}>
        {r.num_uids ?? "—"}<span style={{ color: "var(--text-muted)" }}>/{r.max_uids ?? "—"}</span></span> },
    { key: "links", label: "Links", width: 100,
      render: (r) => (
        <span className="flex gap-2.5 text-[11.5px]">
          {r.github_repo && <a href={r.github_repo} target="_blank" rel="noreferrer"
             className="inline-flex items-center gap-1 hover:underline" style={{ color: "var(--accent)" }}>
            <GitHubMark />github</a>}
          {r.dashboard_url && <a href={r.dashboard_url} target="_blank" rel="noreferrer"
             className="hover:underline" style={{ color: "var(--series-3)" }}>dash</a>}
        </span>
      ) },

    /* ---- off by default; switch on from the Columns picker ---- */
    { key: "price", label: "Price", align: "right", width: 92,
      title: "Alpha token price, in the active currency",
      value: (r) => r.price ?? 0,
      render: (r) => <span className="tnum">
        {unit === "USD" ? (taoUsd ? `$${fmt((r.price ?? 0) * taoUsd, 4)}` : "—") : `τ${(r.price ?? 0).toFixed(6)}`}</span> },
    { key: "earning_miners", label: "Earning UIDs", align: "right", width: 92,
      title: "Miner UIDs with any incentive at all at the last neuron poll (validators excluded)",
      value: (r) => r.earning_miners ?? 0,
      render: (r) => <span className="tnum">{r.earning_miners ?? "—"}</span> },
    { key: "validator_count", label: "Validators", align: "right", width: 84,
      title: "UIDs receiving dividends. A permit alone does not count; the permit count is in the hover.",
      value: (r) => r.validator_uids ?? r.validator_count ?? 0,
      render: (r) => <span className="tnum" title={`${r.validator_count ?? "—"} hold a permit`}>
        {r.validator_uids ?? r.validator_count ?? "—"}</span> },
    { key: "top_coldkey_pct", label: "Top rival %", align: "right", width: 90,
      title: "Share of the subnet's emission taken by its largest coldkey",
      value: (r) => r.top_coldkey_pct ?? 0,
      render: (r) => <span className="tnum">{pct(r.top_coldkey_pct, 1)}</span> },
    { key: "age_days", label: "Age", align: "right", width: 70,
      title: "Days since the subnet was registered",
      value: (r) => r.age_days ?? null,
      render: (r) => <span className="tnum" style={{ color: "var(--text-secondary)" }}>
        {r.age_days === null || r.age_days === undefined ? "—" : `${Math.round(r.age_days)}d`}</span> },
    { key: "uids_free", label: "Free UIDs", align: "right", width: 80,
      title: "Slots nobody has to be deregistered from",
      value: (r) => r.uids_free ?? 0,
      render: (r) => <span className="tnum">{r.uids_free ?? "—"}</span> },
    { key: "validator_tao_per_day", label: "Validators / day", align: "right", width: 110,
      title: "TAO paid to validators per day",
      value: (r) => r.validator_tao_per_day ?? 0,
      render: (r) => <span className="tnum">{moneyCompact(r.validator_tao_per_day, 1)}</span> },
    { key: "owner_tao_per_day", label: "Owner / day", align: "right", width: 96,
      title: "TAO taken by the subnet owner per day",
      value: (r) => r.owner_tao_per_day ?? 0,
      render: (r) => <span className="tnum">{moneyCompact(r.owner_tao_per_day, 1)}</span> },
    { key: "tempo", label: "Tempo", align: "right", width: 90,
      title: "On-chain epoch length: blocks between weight/emission updates (~12s per block)",
      value: (r) => r.tempo ?? 0,
      render: (r) => <span className="tnum">{r.tempo ?? "—"}<span style={{ color: "var(--text-muted)" }}>
        {r.tempo ? ` ≈${Math.round((r.tempo * 12) / 60)}m` : ""}</span></span> },
    { key: "immunity", label: "Immunity", align: "right", width: 90,
      title: "Blocks a newly registered UID cannot be deregistered for",
      value: (r) => r.immunity_period ?? 0,
      render: (r) => <span className="tnum">{r.immunity_period ?? "—"}<span style={{ color: "var(--text-muted)" }}>
        {r.immunity_period ? ` ≈${(r.immunity_period * 12 / 3600).toFixed(0)}h` : ""}</span></span> },
  ];

  const colLabels = useMemo(
    () => Object.fromEntries(allCols.map((col) => [col.key, col.label || col.key])),
    // labels are static
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []);
  const byKey = new Map(allCols.map((col) => [col.key, col]));
  const cols = prefs.order
    .filter((k) => !prefs.hidden.includes(k))
    .map((k) => byKey.get(k))
    .filter((col): col is Col<Row> => Boolean(col));

  return (
    <Chrome block={live.block} taoUsd={taoUsd} connected={live.connected}>
      <PageHeader
        title="Subnets"
        subtitle="Every subnet on finney, updated each block — filter it down to the ones worth mining."
      />

      <div className="grid gap-4 mb-5" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(172px, 1fr))" }}>
        <Stat label="TAO" value={`$${fmt(taoUsd, 2)}`} delta={net?.tao_usd_change ?? null} sub="24h" />
        <Stat label="Subnets" value={net?.subnets ?? "—"} sub="active on finney" />
        <Stat label="Total market cap" value={moneyCompact(net?.mcap_tao, 1)}
              sub={unit === "TAO"
                ? (taoUsd ? `$${compact((net?.mcap_tao ?? 0) * taoUsd, 1)}` : undefined)
                : `τ${compact(net?.mcap_tao, 1)}`} />
        <Stat label="TAO in pools" value={moneyCompact(net?.tao_locked, 1)} sub="AMM reserves" />
        <Stat label="Neurons" value={compact(net?.neurons, 0)}
              sub={`${compact(net?.active_neurons, 0)} setting weights`}
              hint="Neurons that set weights within the subnet's activity_cutoff. This tracks weight-setting, not whether a miner is running." />
        <Stat label="Operators" value={compact(net?.unique_coldkeys, 0)} sub="distinct coldkeys" />
      </div>

      <ActivityFeed />

      <div className="grid gap-4 items-start"
           style={{ gridTemplateColumns: showFilters ? "262px minmax(0,1fr)" : "minmax(0,1fr)" }}>
        {showFilters && (
          <aside className="card p-4 sticky top-[76px] overflow-y-auto"
                 style={{ maxHeight: "calc(100vh - 96px)" }}>
            <div className="text-[12px] font-medium mb-2">Presets</div>
            <div className="flex flex-col gap-1 mb-4">
              {PRESETS.map((p) => (
                <button key={p.name} onClick={() => applyPreset(p)} title={p.hint}
                        className="text-left px-2.5 py-1.5 rounded-lg text-[12.5px] transition-colors"
                        style={{
                          background: activePreset === p.name ? "var(--accent-soft)" : "transparent",
                          color: activePreset === p.name ? "var(--accent)" : "var(--text-secondary)",
                        }}>
                  {p.name}
                </button>
              ))}
            </div>

            <Slider label="Min miner reward / day" unit="τ" value={c.taoDayMin} max={250} step={5}
                    onChange={(v) => set("taoDayMin", v)} />
            <Slider label="Max rivals" value={c.operatorsMax} max={300} step={5}
                    onChange={(v) => set("operatorsMax", v)} atMaxLabel="any" />
            <Slider label="Min miners earning" unit="%" value={c.earningMin} max={60} step={1}
                    onChange={(v) => set("earningMin", v)} />
            <Slider label="Max payback" unit="d" value={c.paybackMax} max={3650} step={5}
                    onChange={(v) => set("paybackMax", v)} atMaxLabel="any" />
            <Slider label="Max reg cost" unit="τ" value={c.regCostMax} max={100} step={0.05}
                    onChange={(v) => set("regCostMax", v)} atMaxLabel="any" decimals={2} />
            <Slider label="Max top-rival share" unit="%" value={c.topCkMax} max={100} step={5}
                    onChange={(v) => set("topCkMax", v)} atMaxLabel="any" />
            <Slider label="Max age" unit="d" value={c.ageMax} max={4000} step={30}
                    onChange={(v) => set("ageMax", v)} atMaxLabel="any" />
            <Slider label="Min free UIDs" value={c.freeUidsMin} max={64} step={1}
                    onChange={(v) => set("freeUidsMin", v)} />

            <div className="flex flex-col gap-1.5 mt-3 text-[12.5px]" style={{ color: "var(--text-secondary)" }}>
              <Check label="Registration open" v={c.openOnly} on={(v) => set("openOnly", v)} />
              <Check label="Only where I mine" v={c.mineOnly} on={(v) => set("mineOnly", v)} />
              <Check label="Only watched" v={c.watchedOnly} on={(v) => set("watchedOnly", v)} />
              <Check label="Pre-launch only" v={c.prelaunchOnly} on={(v) => set("prelaunchOnly", v)} />
            </div>

            <div className="flex gap-2 mt-4">
              <button className="btn btn-ghost flex-1" onClick={() => { setC(BLANK); setActivePreset(null); }}>
                Reset
              </button>
              <button className="btn btn-primary flex-1" onClick={saveCurrent}>Save</button>
            </div>

            {saved.length > 0 && (
              <div className="mt-4">
                <div className="text-[12px] font-medium mb-1.5">Saved</div>
                {saved.map((s) => (
                  <div key={s.id} className="flex items-center gap-2 text-[12.5px] py-0.5">
                    <button className="hover:underline text-left flex-1" style={{ color: "var(--accent)" }}
                            onClick={() => { setC({ ...BLANK, ...s.criteria }); setActivePreset(s.name); }}>
                      {s.name}
                    </button>
                    <button onClick={async () => { await del(`/api/filters/${s.id}`); load(); }}
                            style={{ color: "var(--text-muted)" }} title="Delete">×</button>
                  </div>
                ))}
              </div>
            )}
          </aside>
        )}

        <div className="card">
          <div className="flex items-center gap-3 px-4 py-3 border-b" style={{ borderColor: "var(--border)" }}>
            <button onClick={() => setShowFilters((v) => !v)}
                    className="btn btn-ghost flex items-center gap-1.5">
              {showFilters ? "Hide filters" : "Filters"}
              {n > 0 && (
                <span className="tnum text-[11px] px-1.5 rounded-full"
                      style={{ background: "var(--accent)", color: "#fff" }}>{n}</span>
              )}
            </button>
            <ColumnPicker prefs={prefs} labels={colLabels} locked={LOCKED_COLUMNS}
                          onToggle={toggleCol} onMove={moveCol} onReorder={reorderCol} onReset={resetCols} />
            <input value={c.q} onChange={(e) => set("q", e.target.value)} className="input w-[280px]"
                   placeholder="Filter by name, netuid, or description…" />
            {activePreset && <Pill tone="accent">{activePreset}</Pill>}
            {n > 0 && (
              <button className="text-[12px] hover:underline" style={{ color: "var(--text-muted)" }}
                      onClick={() => { setC(BLANK); setActivePreset(null); }}>clear</button>
            )}
            <span className="ml-auto flex items-center gap-3 text-[12px]" style={{ color: "var(--text-muted)" }}>
              <span title={taoUsd ? `Show every value in TAO or USD · TAO $${fmt(taoUsd, 2)}` : "USD price not available yet"}>
                <Toggle value={unit} onChange={(v) => { if (v !== unit) toggleUnit(); }}
                        options={[{ value: "TAO", label: "τ TAO" }, { value: "USD", label: "$ USD" }]} />
              </span>
              {out.length === merged.length
                ? `${merged.length} subnets`
                : `${out.length} of ${merged.length} subnets`}
            </span>
          </div>
          <DataTable rows={out} cols={cols} initialSort="emission_share" onReorder={reorderCol}
                     rowKey={(r) => r.netuid} maxHeight={720} tieBreak={(r) => r.netuid} />
        </div>
      </div>
    </Chrome>
  );
}

function Slider({ label, value, max, step, onChange, unit = "", atMaxLabel, decimals = 0 }: {
  label: string; value: number; max: number; step: number;
  onChange: (v: number) => void; unit?: string; atMaxLabel?: string; decimals?: number;
}) {
  const atMax = atMaxLabel && value >= max;
  return (
    <div className="mb-2.5">
      <div className="flex justify-between text-[11.5px] mb-1">
        <span style={{ color: "var(--text-secondary)" }}>{label}</span>
        <span className="tnum" style={{ color: atMax ? "var(--text-muted)" : "var(--text-primary)" }}>
          {atMax ? atMaxLabel : `${unit === "τ" ? "τ" : ""}${value.toFixed(decimals)}${unit !== "τ" ? unit : ""}`}
        </span>
      </div>
      <input type="range" className="w-full" min={0} max={max} step={step}
             value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </div>
  );
}

function Check({ label, v, on }: { label: string; v: boolean; on: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 cursor-pointer">
      <input type="checkbox" checked={v} onChange={(e) => on(e.target.checked)} />
      {label}
    </label>
  );
}
