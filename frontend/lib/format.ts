export const fmt = (n: number | null | undefined, d = 2): string =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

export const compact = (n: number | null | undefined, d = 1): string => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(d) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(d) + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(d) + "k";
  return n.toFixed(a < 1 && a > 0 ? Math.max(d, 3) : d);
};

export const pct = (n: number | null | undefined, d = 1): string =>
  n === null || n === undefined || Number.isNaN(n) ? "—" : `${n.toFixed(d)}%`;

export const tao = (n: number | null | undefined, d = 3): string =>
  n === null || n === undefined ? "—" : `τ${compact(n, d)}`;

export const usd = (n: number | null | undefined): string =>
  n === null || n === undefined ? "—" : `$${compact(n, 2)}`;

export const key = (k: string | null | undefined, head = 6, tail = 4): string =>
  !k ? "—" : k.length <= head + tail + 2 ? k : `${k.slice(0, head)}…${k.slice(-tail)}`;

export const ago = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return `${Math.max(0, Math.round(s))}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
};

/** Blocks are ~12s; converts a registration block into an age. */
export const blockAge = (block: number, head: number): string => {
  if (!block || !head || head <= block) return "—";
  const days = ((head - block) * 12) / 86400;
  return days < 1 ? `${(days * 24).toFixed(0)}h` : `${days.toFixed(0)}d`;
};

/** Time until an ISO timestamp, coarse: "3d 2h", "5h 12m", "7m", or "now" once passed. */
export const countdown = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  if (Number.isNaN(s)) return "—";
  if (s <= 0) return "now";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${Math.max(1, m)}m`;
};

/** ISO timestamp as a short UTC stamp for hovers: "Sep 3 14:30 UTC". */
export const stamp = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
                                     hour12: false, timeZone: "UTC" }) + " UTC";
};
