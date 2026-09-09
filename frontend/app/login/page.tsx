"use client";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { api, post } from "@/lib/api";

const ERRORS: Record<string, string> = {
  not_allowed: "That Google account isn't on the allowlist. Ask an admin to add it.",
  disabled: "That account has been disabled.",
  access_denied: "Google sign-in was cancelled.",
};

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [cfg, setCfg] = useState<any>(null);

  useEffect(() => {
    api("/api/auth/config").then(setCfg).catch(() => {});
    const e = params.get("error");
    if (e) setErr(ERRORS[e] ?? e);
  }, [params]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      await post("/api/auth/login", { email, password });
      router.push("/");
    } catch (e: any) {
      setErr(e?.status === 429
        ? "Too many failed attempts. Try again in 15 minutes."
        : "Incorrect email or password.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4"
         style={{ background: "var(--plane)" }}>
      <div className="w-full max-w-[380px]">
        <div className="text-center mb-6">
          <div className="text-[22px] font-semibold tracking-tight">
            Tao<span style={{ color: "var(--accent)" }}>Scope</span>
          </div>
          <p className="mt-1 text-[13px]" style={{ color: "var(--text-muted)" }}>
            Bittensor subnet &amp; miner intelligence
          </p>
        </div>

        <div className="card p-6">
          {/* Google */}
          <button
            type="button"
            disabled={!cfg?.google_enabled}
            onClick={() => { window.location.href = "/api/auth/google/start"; }}
            title={cfg?.google_enabled ? "" : cfg?.reason ?? ""}
            className="w-full flex items-center justify-center gap-2.5 py-2.5 rounded-lg text-[13.5px] font-medium transition-colors disabled:opacity-45 disabled:cursor-not-allowed"
            style={{ border: "1px solid var(--border-strong)", background: "var(--surface-1)" }}
          >
            <svg width="17" height="17" viewBox="0 0 48 48" aria-hidden>
              <path fill="#4285F4" d="M45.1 24.5c0-1.6-.1-2.7-.4-3.9H24v7.1h12.1c-.2 1.8-1.6 4.6-4.5 6.4l6.9 5.3c4.1-3.8 6.6-9.4 6.6-15z"/>
              <path fill="#34A853" d="M24 46c5.9 0 10.9-2 14.5-5.3l-6.9-5.3c-1.8 1.3-4.3 2.2-7.6 2.2-5.8 0-10.7-3.8-12.5-9.1l-7.1 5.5C8.1 41.1 15.4 46 24 46z"/>
              <path fill="#FBBC05" d="M11.5 28.5c-.5-1.4-.7-2.9-.7-4.5s.3-3.1.7-4.5l-7.1-5.5C2.9 17 2 20.4 2 24s.9 7 2.4 10l7.1-5.5z"/>
              <path fill="#EA4335" d="M24 10.6c4.1 0 6.9 1.8 8.5 3.3l6.2-6C34.9 4.500 29.9 2 24 2 15.4 2 8.1 6.9 4.4 14l7.1 5.5c1.8-5.3 6.7-8.9 12.5-8.9z"/>
            </svg>
            Continue with Google
          </button>

          {!cfg?.google_enabled && cfg?.reason && (
            <p className="mt-2 text-[11.5px] text-center" style={{ color: "var(--text-muted)" }}>
              Google sign-in activates once a domain is configured.
            </p>
          )}

          <div className="flex items-center gap-3 my-5">
            <span className="flex-1 h-px" style={{ background: "var(--border)" }} />
            <span className="text-[11.5px]" style={{ color: "var(--text-muted)" }}>or</span>
            <span className="flex-1 h-px" style={{ background: "var(--border)" }} />
          </div>

          <form onSubmit={submit}>
            <label className="block text-[12px] mb-1" style={{ color: "var(--text-secondary)" }}>
              Email
            </label>
            <input className="input w-full" type="email" required autoComplete="username"
                   value={email} onChange={(e) => setEmail(e.target.value)} />

            <label className="block text-[12px] mt-3 mb-1" style={{ color: "var(--text-secondary)" }}>
              Password
            </label>
            <input className="input w-full" type="password" required autoComplete="current-password"
                   value={password} onChange={(e) => setPassword(e.target.value)} />

            {err && (
              <div className="mt-3 text-[12px] px-3 py-2 rounded-lg" role="alert"
                   style={{ color: "var(--delta-down)",
                            background: "color-mix(in srgb, var(--critical) 8%, transparent)" }}>
                {err}
              </div>
            )}

            <button type="submit" disabled={busy}
                    className="btn btn-primary w-full mt-4 py-2.5 disabled:opacity-60">
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}

export default function Login() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
