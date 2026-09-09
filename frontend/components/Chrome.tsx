"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Logo } from "@/components/Logo";
import { api, post } from "@/lib/api";
import { fmt } from "@/lib/format";
import { useCurrency } from "@/lib/ui";

const NAV = [
  { href: "/", label: "Subnets" },
  { href: "/operators", label: "Operators" },
  { href: "/my-work", label: "My work" },
  { href: "/market", label: "Market" },
  { href: "/settings", label: "Settings" },
];

export function Chrome({
  children, block, taoUsd, connected,
}: {
  children: React.ReactNode; block?: number; taoUsd?: number; connected?: boolean;
}) {
  const path = usePathname();
  const router = useRouter();
  const { unit, toggle, setTaoUsd } = useCurrency();
  const [theme, setTheme] = useState("light");
  const [me, setMe] = useState<any>(null);

  useEffect(() => {
    setTheme(localStorage.getItem("taoscope-theme") || "light");
    api("/api/auth/me").then(setMe).catch(() => {});
  }, []);

  useEffect(() => { if (taoUsd) setTaoUsd(taoUsd); }, [taoUsd, setTaoUsd]);

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    localStorage.setItem("taoscope-theme", next);
    document.documentElement.setAttribute("data-theme", next);
  };

  const logout = async () => {
    await post("/api/auth/logout").catch(() => {});
    router.push("/login");
  };

  return (
    <div className="min-h-screen">
      <header
        className="sticky top-0 z-50 flex items-center gap-1 px-7 h-[60px] border-b backdrop-blur"
        style={{
          background: "color-mix(in srgb, var(--surface-1) 88%, transparent)",
          borderColor: "var(--border)",
        }}
      >
        <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight text-[15px] mr-4">
          <Logo size={22} className="rounded-[5px]" />
          <span>Tao<span style={{ color: "var(--accent)" }}>Scope</span></span>
        </Link>

        <nav className="flex items-center gap-0.5">
          {NAV.map((n) => {
            const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
            return (
              <Link
                key={n.href}
                href={n.href}
                className="px-3 py-1.5 rounded-lg text-[13px] font-medium transition-colors"
                style={{
                  background: active ? "var(--accent-soft)" : "transparent",
                  color: active ? "var(--accent)" : "var(--text-secondary)",
                }}
              >
                {n.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3 text-[12px]">
          <button
            onClick={toggle}
            className="px-2.5 py-1 rounded-lg font-medium tnum"
            style={{ background: "var(--surface-2)", color: "var(--text-secondary)" }}
            title="Switch every value between TAO and USD"
          >
            {unit === "TAO" ? "τ TAO" : "$ USD"}
          </button>

          {taoUsd ? (
            <span className="tnum" style={{ color: "var(--text-muted)" }}>
              TAO ${fmt(taoUsd, 2)}
            </span>
          ) : null}

          {block ? (
            <span className="tnum hidden lg:inline" style={{ color: "var(--text-muted)" }}>
              #{block.toLocaleString()}
            </span>
          ) : null}

          <span className="flex items-center gap-1.5" style={{ color: "var(--text-muted)" }}
                title={connected ? "Live websocket connected" : "Reconnecting…"}>
            <span className="inline-block w-[7px] h-[7px] rounded-full"
                  style={{ background: connected ? "var(--good)" : "var(--critical)" }} aria-hidden />
            {connected ? "live" : "offline"}
          </span>

          <button onClick={toggleTheme} className="opacity-60 hover:opacity-100 px-1" title="Theme">
            {theme === "dark" ? "☀" : "☾"}
          </button>

          <button onClick={logout}
                  className="opacity-70 hover:opacity-100"
                  title={me?.email}>
            {me?.display_name?.split(" ")[0] ?? "Sign out"}
          </button>
        </div>
      </header>

      <main className="px-7 py-6 max-w-[1720px] mx-auto">{children}</main>
    </div>
  );
}
