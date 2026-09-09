"use client";
import Link from "next/link";
import { useEffect, useState } from "react";

import { BarList } from "@/components/Charts";
import { Chrome } from "@/components/Chrome";
import { PageHeader } from "@/components/PageHeader";
import { Col, DataTable } from "@/components/Table";
import { api } from "@/lib/api";
import { compact, fmt, pct } from "@/lib/format";
import { useCurrency } from "@/lib/ui";
import { useLive } from "@/lib/live";

type Row = Record<string, any>;

export default function Market() {
  const live = useLive();
  const { moneyCompact } = useCurrency();
  const [movers, setMovers] = useState<Row[]>([]);
  const [window, setWindow] = useState("1h");
  const [netuid, setNetuid] = useState(64);
  const [amount, setAmount] = useState(100);
  const [quote, setQuote] = useState<Row | null>(null);
  const [depth, setDepth] = useState<Row | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api(`/api/market/movers?window=${window}&limit=12`)
      .then((d) => setMovers(d.movers)).catch(() => setMovers([]));
  }, [window]);

  useEffect(() => {
    setErr("");
    Promise.all([
      api(`/api/market/quote/${netuid}?amount=${amount}&side=buy`),
      api(`/api/market/depth/${netuid}`),
    ])
      .then(([q, d]) => { setQuote(q); setDepth(d); })
      .catch(() => { setErr("No pool data for that subnet."); setQuote(null); setDepth(null); });
  }, [netuid, amount]);

  const inputStyle = {};

  const ladderCols: Col<Row>[] = [
    { key: "tao", label: "Order (τ)", align: "right", value: (r) => r.tao,
      render: (r) => <span className="tnum">{r.tao}</span> },
    { key: "alpha_out", label: "Alpha received", align: "right", value: (r) => r.alpha_out,
      render: (r) => <span className="tnum">{fmt(r.alpha_out, 2)}</span> },
    { key: "exec_price", label: "Exec price", align: "right", value: (r) => r.exec_price,
      render: (r) => <span className="tnum">{r.exec_price.toFixed(6)}</span> },
    { key: "slippage_pct", label: "Slippage", align: "right", value: (r) => r.slippage_pct,
      render: (r) => {
        const v = r.slippage_pct;
        const c = v > 5 ? "var(--critical)" : v > 1 ? "var(--serious)" : "var(--text-secondary)";
        return <span className="tnum" style={{ color: c }}>{pct(v, 3)}</span>;
      } },
  ];

  return (
    <Chrome block={live.block} taoUsd={live.taoUsd} connected={live.connected}>
      <PageHeader
        title="Market"
        subtitle="Alpha prices, pool depth and true execution cost — quotes use the same constant-product curve the chain does."
      />

      <div className="grid gap-3" style={{ gridTemplateColumns: "minmax(0,1fr) minmax(0,1.1fr)" }}>
        <div className="card p-3">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-[13px] font-medium">Movers</h2>
            <div className="flex gap-1">
              {["1h", "24h", "7d"].map((w) => (
                <button key={w} onClick={() => setWindow(w)}
                        className="px-2 py-0.5 rounded text-[11px]"
                        style={{
                          background: window === w ? "rgba(127,127,127,0.16)" : "transparent",
                          color: window === w ? "var(--text-primary)" : "var(--text-muted)",
                        }}>{w}</button>
              ))}
            </div>
          </div>
          {movers.length === 0 ? (
            <p className="text-[12px]" style={{ color: "var(--text-muted)" }}>
              Building history — movers appear once the collector has {window} of data.
            </p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {movers.map((m) => {
                const up = (m.pct_change ?? 0) >= 0;
                return (
                  <Link key={m.netuid} href={`/subnet/${m.netuid}`}
                        className="grid items-center gap-3 text-[12px] hover:underline"
                        style={{ gridTemplateColumns: "132px 1fr 68px" }}>
                    <span className="truncate">
                      <span className="tnum" style={{ color: "var(--text-muted)" }}>{m.netuid}</span>{" "}
                      {m.name}
                    </span>
                    <span className="relative h-[12px]">
                      <span className="absolute top-0 bottom-0" style={{
                        left: "50%", width: 1, background: "var(--axis)",
                      }} />
                      <span className="absolute top-0 bottom-0" style={{
                        [up ? "left" : "right"]: "50%",
                        width: `${Math.min(50, Math.abs(m.pct_change ?? 0) * 2)}%`,
                        background: up ? "var(--delta-up)" : "var(--delta-down)",
                        borderRadius: 3,
                      } as any} />
                    </span>
                    <span className="tnum text-right"
                          style={{ color: up ? "var(--delta-up)" : "var(--delta-down)" }}>
                      {up ? "+" : ""}{fmt(m.pct_change, 2)}%
                    </span>
                  </Link>
                );
              })}
            </div>
          )}
        </div>

        <div className="card p-3">
          <h2 className="text-[13px] font-medium mb-3">Order cost calculator</h2>
          <div className="flex items-end gap-3 mb-3">
            <div>
              <label className="text-[11.5px]" style={{ color: "var(--text-secondary)" }}>Subnet</label>
              <input type="number" value={netuid} min={1} max={128}
                     onChange={(e) => setNetuid(Number(e.target.value))}
                     className="input mt-1 w-[90px] tnum"
                     style={inputStyle} />
            </div>
            <div>
              <label className="text-[11.5px]" style={{ color: "var(--text-secondary)" }}>Buy with τ</label>
              <input type="number" value={amount} min={0.1} step={10}
                     onChange={(e) => setAmount(Number(e.target.value))}
                     className="input mt-1 w-[120px] tnum"
                     style={inputStyle} />
            </div>
          </div>

          {err && <p className="text-[12px]" style={{ color: "var(--critical)" }}>{err}</p>}

          {quote && (
            <div className="grid gap-3 mb-3" style={{ gridTemplateColumns: "repeat(4,1fr)" }}>
              <Field label="Spot" value={quote.spot_price.toFixed(6)} />
              <Field label="Exec price" value={quote.exec_price.toFixed(6)} />
              <Field label="Alpha out" value={fmt(quote.alpha_delta, 2)} />
              <Field label="Slippage" value={pct(quote.slippage_pct, 3)}
                     color={quote.slippage_pct > 5 ? "var(--critical)"
                          : quote.slippage_pct > 1 ? "var(--serious)" : "var(--text-primary)"} />
            </div>
          )}

          {depth && (
            <>
              <div className="text-[11.5px] mb-1" style={{ color: "var(--text-muted)" }}>
                Pool: {moneyCompact(depth.tao_in, 1)} / {compact(depth.alpha_in, 1)} α
              </div>
              <DataTable rows={depth.ladder} cols={ladderCols} initialSort="tao"
                         initialDesc={false} rowKey={(r) => r.tao} maxHeight={300} />
            </>
          )}
        </div>
      </div>
    </Chrome>
  );
}

function Field({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="text-[11px]" style={{ color: "var(--text-muted)" }}>{label}</div>
      <div className="tnum text-[14px] mt-0.5" style={{ color: color ?? "var(--text-primary)" }}>{value}</div>
    </div>
  );
}
