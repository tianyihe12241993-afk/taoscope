"use client";
import { useCallback, useEffect, useState } from "react";

import { Chrome } from "@/components/Chrome";
import { PageHeader } from "@/components/PageHeader";
import { api, del, post, put } from "@/lib/api";
import { ago } from "@/lib/format";
import { useLive } from "@/lib/live";
import { Pill } from "@/lib/ui";

type Row = Record<string, any>;

export default function Settings() {
  const live = useLive();
  const [me, setMe] = useState<Row | null>(null);
  const [cfg, setCfg] = useState<Row | null>(null);
  const [tg, setTg] = useState<Row | null>(null);
  const [allow, setAllow] = useState<Row | null>(null);
  const [aiCfg, setAiCfg] = useState<Row | null>(null);
  const [sessions, setSessions] = useState<Row[]>([]);
  const [wallets, setWallets] = useState<Row[]>([]);
  const [newEmail, setNewEmail] = useState("");
  const [walletAddr, setWalletAddr] = useState("");
  const [challenge, setChallenge] = useState("");

  const load = useCallback(() => {
    api("/api/auth/me").then(setMe).catch(() => {});
    api("/api/auth/config").then(setCfg).catch(() => {});
    api("/api/me/telegram").then(setTg).catch(() => {});
    api("/api/me/ai").then(setAiCfg).catch(() => {});
    api("/api/auth/sessions").then((d) => setSessions(d.sessions)).catch(() => {});
    api("/api/me/wallets").then((d) => setWallets(d.wallets)).catch(() => {});
    api("/api/auth/allowlist").then(setAllow).catch(() => setAllow(null));
  }, []);

  useEffect(load, [load]);

  const isAdmin = me?.role === "admin";

  return (
    <Chrome block={live.block} taoUsd={live.taoUsd} connected={live.connected}>
      <PageHeader title="Settings" subtitle="Access, notifications and connected accounts." />

      <div className="grid gap-4 items-start" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(400px,1fr))" }}>

        {/* ---------- account ---------- */}
        <Section title="Account">
          <Field k="Email" v={me?.email} />
          <Field k="Role" v={me?.role} />
          <Field k="Last sign-in" v={ago(me?.last_login)} />
        </Section>

        {/* ---------- google ---------- */}
        <Section title="Google sign-in">
          {cfg?.google_enabled ? (
            <p className="text-[12.5px]" style={{ color: "var(--delta-up)" }}>
              Enabled. Allowlisted Google accounts can sign in.
            </p>
          ) : (
            <>
              <div className="mb-2"><Pill tone="warn">not configured</Pill></div>
              <p className="text-[12.5px] mb-2" style={{ color: "var(--text-secondary)" }}>
                Google refuses bare IP addresses as redirect URIs, so this needs a real
                domain first. Once you have one:
              </p>
              <ol className="text-[12px] leading-relaxed pl-4 list-decimal"
                  style={{ color: "var(--text-secondary)" }}>
                <li>Point an A record at this server&apos;s public IP</li>
                <li>Set <code>SITE_ADDRESS</code> in <code>.env</code>, restart Caddy (TLS is automatic)</li>
                <li>In Google Cloud Console → Credentials → OAuth client (Web)</li>
                <li>Authorised redirect URI:{" "}
                    <code>https://YOUR-DOMAIN/api/auth/google/callback</code></li>
                <li>Put the client id/secret and <code>TAOSCOPE_PUBLIC_URL</code> in <code>.env</code>,
                    set <code>TAOSCOPE_COOKIE_SECURE=true</code>, restart the backend</li>
              </ol>
            </>
          )}
        </Section>

        {/* ---------- allowlist ---------- */}
        {isAdmin && (
          <Section title="Who can sign in">
            <p className="text-[12px] mb-2" style={{ color: "var(--text-secondary)" }}>
              Sign-up is closed. Only these Google accounts are accepted.
            </p>
            <form className="flex gap-2 mb-3"
                  onSubmit={async (e) => {
                    e.preventDefault();
                    if (!newEmail.trim()) return;
                    await post("/api/auth/allowlist", { email: newEmail.trim(), role: "member" });
                    setNewEmail(""); load();
                  }}>
              <input className="input flex-1" placeholder="teammate@gmail.com" type="email"
                     value={newEmail} onChange={(e) => setNewEmail(e.target.value)} />
              <button className="btn btn-primary">Add</button>
            </form>
            <div className="flex flex-col gap-1">
              {(allow?.allowlist ?? []).map((a: Row) => (
                <div key={a.email} className="flex items-center gap-2 text-[12.5px] px-2 py-1.5 rounded-lg"
                     style={{ background: "var(--surface-2)" }}>
                  <span className="flex-1 truncate">{a.email}</span>
                  <Pill tone={a.role === "admin" ? "accent" : "neutral"}>{a.role}</Pill>
                  <button style={{ color: "var(--text-muted)" }} title="Remove"
                          onClick={async () => { await del(`/api/auth/allowlist/${a.email}`); load(); }}>×</button>
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* ---------- telegram ---------- */}
        <Section title="Telegram">
          {!tg?.enabled ? (
            <>
              <div className="mb-2"><Pill tone="warn">no bot token</Pill></div>
              <p className="text-[12.5px] mb-2" style={{ color: "var(--text-secondary)" }}>
                Long-polling, so it needs no domain and no open port. About five minutes:
              </p>
              <ol className="text-[12px] leading-relaxed pl-4 list-decimal"
                  style={{ color: "var(--text-secondary)" }}>
                <li>In Telegram open{" "}
                  <a className="underline" href="https://t.me/BotFather" target="_blank"
                     rel="noreferrer" style={{ color: "var(--accent)" }}>@BotFather</a>{" "}
                  and send <code>/newbot</code>. Pick a name, then a username ending in{" "}
                  <code>bot</code>. It replies with a token.</li>
                <li>Put it in <code>.env</code> as <code>TAOSCOPE_TELEGRAM_BOT_TOKEN</code>,
                    then <code>docker compose up -d backend</code></li>
                <li>Check <code>curl -s localhost/api/status | grep telegram</code> —
                    it should say <code>online @yourbot</code></li>
                <li>Come back here, click <b>Generate code</b>, and send{" "}
                    <code>/link CODE</code> to your bot</li>
              </ol>
            </>
          ) : tg.linked ? (
            <>
              <p className="text-[12.5px] mb-2" style={{ color: "var(--delta-up)" }}>
                Linked{tg.chat_username ? ` to @${tg.chat_username}` : ""}.
              </p>
              <p className="text-[12px] mb-2" style={{ color: "var(--text-secondary)" }}>
                Try <code>/me</code>, <code>/sn 64</code>, <code>/ck 5Abc…</code>, <code>/top</code>.
              </p>
              <NotifyPrefs prefs={tg.prefs ?? {}} onSaved={load} />
              <button className="btn btn-ghost mt-3"
                      onClick={async () => { await del("/api/me/telegram"); load(); }}>Unlink</button>
            </>
          ) : (
            <>
              <p className="text-[12.5px] mb-2" style={{ color: "var(--text-secondary)" }}>
                {tg.link_code
                  ? <>Send <code>/link {tg.link_code}</code> to{" "}
                     {tg.bot_username ? <b>@{tg.bot_username}</b> : "your bot"} in Telegram.</>
                  : "Generate a code, then send it to the bot to connect this account."}
              </p>
              <button className="btn btn-primary"
                      onClick={async () => { await post("/api/me/telegram/code"); load(); }}>
                {tg.link_code ? "New code" : "Generate code"}
              </button>
            </>
          )}
        </Section>

        {/* ---------- ai q&a ---------- */}
        <Section title="Ask the bot">
          {aiCfg?.enabled ? (
            <>
              <p className="text-[12.5px] mb-2" style={{ color: "var(--delta-up)" }}>
                Active — {aiCfg.model} via {aiCfg.provider}, {aiCfg.effort} effort.
              </p>
              <p className="text-[12px] mb-2" style={{ color: "var(--text-secondary)" }}>
                In the direct chat just type a question. In a group use{" "}
                <code>/ask your question</code> — a bare @mention only works if you turn
                privacy mode off in BotFather (<code>/setprivacy</code> → Disable, then
                re-add the bot to the group).
              </p>
              <div className="grid gap-2 text-[12px]" style={{ gridTemplateColumns: "1fr 1fr" }}>
                <Field k="Answered (24h)" v={`${aiCfg.last_24h?.answered ?? 0} / ${aiCfg.daily_limit}`} />
                <Field k="Spend (24h)" v={`$${(aiCfg.last_24h?.spend_usd ?? 0).toFixed(2)}`} />
                <Field k="Answered (30d)" v={aiCfg.last_30d?.answered ?? 0} />
                <Field k="Spend (30d)" v={`$${(aiCfg.last_30d?.spend_usd ?? 0).toFixed(2)}`} />
              </div>
            </>
          ) : (
            <>
              <div className="mb-2"><Pill tone="warn">no API key</Pill></div>
              <p className="text-[12.5px] mb-2" style={{ color: "var(--text-secondary)" }}>
                Lets you ask the bot questions in plain English — it answers from live
                chain data using read-only tools, never guesses a number.
              </p>
              <ol className="text-[12px] leading-relaxed pl-4 list-decimal"
                  style={{ color: "var(--text-secondary)" }}>
                <li>Put an OpenRouter key in <code>.env</code> as{" "}
                    <code>TAOSCOPE_OPENROUTER_API_KEY</code> (or an Anthropic key as{" "}
                    <code>TAOSCOPE_ANTHROPIC_API_KEY</code>)</li>
                <li><code>docker compose up -d backend</code></li>
              </ol>
              <p className="text-[11.5px] mt-2" style={{ color: "var(--text-muted)" }}>
                Roughly $0.03–0.04 per question. Capped at {aiCfg?.daily_limit ?? 100} answers
                per chat per day; spend shows here once it is running.
              </p>
            </>
          )}
        </Section>

        {/* ---------- wallet ---------- */}
        <Section title="Wallet">
          <div className="mb-2"><Pill tone="neutral">verification coming</Pill></div>
          <p className="text-[12.5px] mb-2" style={{ color: "var(--text-secondary)" }}>
            You can register an address and get a signing challenge now. Signature
            verification and any on-chain action land with the trading release — the key
            will live in a separate signing service, never in this web app.
          </p>
          <div className="flex gap-2 mb-2">
            <input className="input flex-1" placeholder="5… wallet address"
                   value={walletAddr} onChange={(e) => setWalletAddr(e.target.value)} />
            <button className="btn btn-ghost" onClick={async () => {
              if (!walletAddr.trim()) return;
              const r = await post("/api/me/wallets/challenge", { ss58: walletAddr.trim() });
              setChallenge(r.challenge); setWalletAddr(""); load();
            }}>Get challenge</button>
          </div>
          {challenge && (
            <div className="text-[11.5px] p-2 rounded-lg tnum break-all"
                 style={{ background: "var(--surface-2)", color: "var(--text-secondary)" }}>
              {challenge}
            </div>
          )}
          {wallets.map((w) => (
            <div key={w.id} className="text-[12px] tnum mt-1.5" style={{ color: "var(--text-secondary)" }}>
              {w.ss58.slice(0, 12)}…{w.ss58.slice(-6)} —{" "}
              {w.verified_at ? "verified" : "awaiting signature"}
            </div>
          ))}
        </Section>

        {/* ---------- sessions ---------- */}
        <Section title="Active sessions">
          <div className="flex flex-col gap-1">
            {sessions.filter((s) => !s.revoked_at).map((s) => (
              <div key={s.jti} className="flex items-center gap-2 text-[12px] px-2 py-1.5 rounded-lg"
                   style={{ background: "var(--surface-2)" }}>
                <span className="flex-1 truncate" style={{ color: "var(--text-secondary)" }}>
                  {s.ip} · {ago(s.issued_at)}
                </span>
                {s.current ? <Pill tone="good">this device</Pill> : (
                  <button style={{ color: "var(--delta-down)" }}
                          onClick={async () => { await del(`/api/auth/sessions/${s.jti}`); load(); }}>
                    revoke
                  </button>
                )}
              </div>
            ))}
          </div>
        </Section>
      </div>
    </Chrome>
  );
}

const EVENT_KINDS: { k: string; label: string; hint: string }[] = [
  { k: "new_subnet",     label: "New subnets",     hint: "A netuid is registered on the network" },
  { k: "subnet_started", label: "Subnet goes live", hint: "A registered subnet makes its start call and begins paying" },
  { k: "king_change",  label: "Top-earner changes", hint: "The coldkey taking the biggest slice of a subnet changes" },
  { k: "registration", label: "Registration flips", hint: "A subnet opens or closes registration" },
  { k: "my_miners",    label: "My miners",          hint: "One of your UIDs gets deregistered" },
  { k: "price_alert",  label: "Price alerts",       hint: "Thresholds you set on the Market page" },
  { k: "emission_move",label: "Big emission moves", hint: "A subnet's share shifts sharply (noisy)" },
];

function NotifyPrefs({ prefs, onSaved }: { prefs: Record<string, boolean>; onSaved: () => void }) {
  const [local, setLocal] = useState(prefs);
  const flip = async (k: string, v: boolean) => {
    setLocal((p) => ({ ...p, [k]: v }));
    await put("/api/me/telegram/prefs", { [k]: v });
    onSaved();
  };
  return (
    <div className="mt-3">
      <div className="text-[12px] font-medium mb-1.5">Notify me about</div>
      <div className="flex flex-col gap-1">
        {EVENT_KINDS.map((e) => (
          <label key={e.k} title={e.hint}
                 className="flex items-center gap-2 text-[12.5px] cursor-pointer"
                 style={{ color: "var(--text-secondary)" }}>
            <input type="checkbox"
                   checked={local[e.k] ?? (e.k !== "emission_move")}
                   onChange={(ev) => flip(e.k, ev.target.checked)} />
            {e.label}
          </label>
        ))}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card p-4">
      <h2 className="text-[13.5px] font-medium mb-3">{title}</h2>
      {children}
    </div>
  );
}

function Field({ k, v }: { k: string; v: any }) {
  return (
    <div className="flex justify-between text-[12.5px] py-1">
      <span style={{ color: "var(--text-muted)" }}>{k}</span>
      <span>{v ?? "—"}</span>
    </div>
  );
}
