"use client";
import { ReactNode } from "react";

export function PageHeader({ title, subtitle, right }: {
  title: string; subtitle?: string; right?: ReactNode;
}) {
  return (
    <div className="flex items-end gap-4 mb-5">
      <div>
        <h1 className="text-[20px] font-semibold tracking-[-0.01em] leading-tight">{title}</h1>
        {subtitle && (
          <p className="text-[13px] mt-1" style={{ color: "var(--text-secondary)" }}>{subtitle}</p>
        )}
      </div>
      {right && <div className="ml-auto flex items-center gap-2">{right}</div>}
    </div>
  );
}
