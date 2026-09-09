"use client";
import { ReactNode } from "react";

export function PageHeader({ title, subtitle, right }: {
  title: string; subtitle?: string; right?: ReactNode;
}) {
  return (
    <div className="flex items-end gap-4 mb-4">
      <div className="flex items-stretch gap-2.5">
        {/* accent rule: marks the page title the way a terminal marks the
            active instrument, without spending a whole heading row on it */}
        <span className="w-[3px] rounded-full shrink-0"
              style={{ background: "var(--accent)" }} aria-hidden />
        <div>
          <h1 className="text-[18px] font-semibold tracking-[-0.015em] leading-tight">{title}</h1>
          {subtitle && (
            <p className="text-[12.5px] mt-0.5" style={{ color: "var(--text-secondary)" }}>{subtitle}</p>
          )}
        </div>
      </div>
      {right && <div className="ml-auto flex items-center gap-2">{right}</div>}
    </div>
  );
}
