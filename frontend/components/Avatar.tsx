"use client";
import { useState } from "react";

/**
 * Subnet avatar: the on-chain logo when the owner published one and it loads,
 * otherwise a coloured initial. The hue is derived from the netuid so a subnet
 * keeps the same colour everywhere; the hsl values read on both themes.
 */
export function Avatar({ src, name, seed, size = 18 }: {
  src?: string | null; name?: string | null; seed: number; size?: number;
}) {
  const [broken, setBroken] = useState(false);
  const hue = Math.round((seed * 137.508) % 360);
  const letter = (name || "?").trim().charAt(0).toUpperCase() || "?";

  if (src && !broken) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img src={src} alt="" width={size} height={size} loading="lazy" decoding="async"
           referrerPolicy="no-referrer" onError={() => setBroken(true)}
           className="rounded-full shrink-0 object-cover"
           style={{ width: size, height: size, background: "var(--surface-2)" }} />
    );
  }
  return (
    <span aria-hidden className="inline-flex items-center justify-center rounded-full shrink-0 font-semibold"
          style={{ width: size, height: size, fontSize: Math.round(size * 0.55), lineHeight: 1,
                   background: `hsl(${hue} 55% 50% / 0.22)`, color: `hsl(${hue} 55% 42%)` }}>
      {letter}
    </span>
  );
}
