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

/** An instrument readout: micro caption over a mono figure. */
function Readout({ label, children, title }: {
  label: string; children: React.ReactNode; title?: string;
}) {
  return (
    <div className="hidden md:flex flex-col items-end leading-none gap-[3px]" title={title}>
      <span className="label-micro" style={{ fontSize: 9 }}>{label}</span>
      <span className="tnum text-[12.5px]" style={{ color: "var(--text-primary)" }}>{children}</span>
    </div>
  );
}

function Divider() {
  return <span className="hidden md:block w-px h-7" style={{ background: "var(--border)" }} aria-hidden />;
}

export function Chrome({
  children, block, taoUsd, connected,
}: {
  children: React.ReactNode; block?: number; taoUsd?: number; connected?: boolean;
}) {
  const path = usePathname();
  const router = useRouter();
  const { unit, toggle, setTaoUsd } = useCurrency();
  const [theme, setTheme] = useState("dark");
  const [me, setMe] = useState<any>(null);

  useEffect(() => {
    setTheme(localStorage.getItem("taoscope-theme") || "dark");
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
      {/* The header floats: translucent + blurred, so rows scroll through it
          rather than under a slab. `.glass` falls back to the solid surface
          where backdrop-filter is unsupported. */}
      <header
        className="glass sticky top-0 z-50 flex items-stretch h-[54px] border-b"
        style={{ borderColor: "var(--border)" }}
      >
        <Link
          href="/"
          className="flex items-center gap-2 font-semibold tracking-tight text-[14px] pl-5 pr-4 shrink-0"
        >
          <Logo size={20} className="rounded-[4px]" />
          <span>Tao<span style={{ color: "var(--accent)" }}>Scope</span></span>
        </Link>

        {/* Terminal tabs: the active view is marked by a lit rule on the bar's
            own bottom edge, so the header reads as one continuous strip. */}
        <nav className="flex items-stretch">
          {NAV.map((n) => {
            const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
            return (
              <Link
                key={n.href}
                href={n.href}
                className="relative flex items-center px-3.5 text-[12.5px] font-medium transition-colors hover:!text-[color:var(--text-primary)]"
                style={{ color: active ? "var(--text-primary)" : "var(--text-secondary)" }}
              >
                {n.label}
                <span
                  className="absolute left-2 right-2 bottom-0 h-[2px] rounded-t-sm transition-all duration-200"
                  style={{
                    background: active ? "var(--accent)" : "transparent",
                    boxShadow: active
                      ? "0 0 10px color-mix(in srgb, var(--accent) 65%, transparent)"
                      : "none",
                  }}
                  aria-hidden
                />
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3.5 pr-5">
          {taoUsd ? <Readout label="TAO/USD" title="Spot TAO price in USD">${fmt(taoUsd, 2)}</Readout> : null}

          {block ? (
            <Readout label="Block" title="Latest finney block seen by the collector">
              {block.toLocaleString()}
            </Readout>
          ) : null}

          <Divider />

          <button
            onClick={toggle}
            className="tnum text-[11.5px] font-semibold px-2 py-[5px] rounded-[4px] transition-colors"
            style={{
              background: "var(--surface-2)",
              color: "var(--text-secondary)",
              border: "1px solid var(--border)",
            }}
            title="Switch every value between TAO and USD"
          >
            {unit === "TAO" ? "τ TAO" : "$ USD"}
          </button>

          <span
            className="flex items-center gap-1.5 label-micro"
            style={{ fontSize: 9.5 }}
            title={connected ? "Live websocket connected" : "Reconnecting…"}
          >
            <span className="live-dot" data-off={connected ? "false" : "true"} aria-hidden />
            {connected ? "Live" : "Offline"}
          </span>

          <Divider />

          <button
            onClick={toggleTheme}
            className="opacity-55 hover:opacity-100 transition-opacity text-[13px]"
            title={theme === "dark" ? "Switch to light" : "Switch to dark"}
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>

          <button
            onClick={logout}
            className="text-[12px] opacity-70 hover:opacity-100 transition-opacity"
            title={me?.email ? `Sign out — ${me.email}` : "Sign out"}
          >
            {me?.display_name?.split(" ")[0] ?? "Sign out"}
          </button>
        </div>
      </header>

      <main className="px-6 py-5 max-w-[1760px] mx-auto">{children}</main>
    </div>
  );
}
