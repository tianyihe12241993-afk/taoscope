"use client";
import { ReactNode, useState } from "react";
import { createPortal } from "react-dom";

/**
 * Hover card anchored to its child. Rendered through a portal with fixed
 * positioning so the table's overflow container never clips it. Flips above
 * the anchor when there is no room below.
 */
export function Tooltip({ text, children, width = 340 }: {
  text?: string | null; children: ReactNode; width?: number;
}) {
  const [pos, setPos] = useState<{ x: number; y: number; above: boolean } | null>(null);

  const show = (el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    const above = r.bottom + 120 > window.innerHeight;
    setPos({
      x: Math.max(8, Math.min(r.left, window.innerWidth - width - 12)),
      y: above ? r.top - 6 : r.bottom + 6,
      above,
    });
  };
  const hide = () => setPos(null);

  return (
    <>
      <span className="inline-flex min-w-0 max-w-full"
            onMouseEnter={(e) => show(e.currentTarget)} onMouseLeave={hide}
            onFocus={(e) => show(e.currentTarget)} onBlur={hide}>
        {children}
      </span>
      {pos && text && typeof document !== "undefined" && createPortal(
        <div role="tooltip" style={{
          position: "fixed", left: pos.x, top: pos.y, width, zIndex: 100,
          transform: pos.above ? "translateY(-100%)" : undefined,
          background: "var(--surface-1)", border: "1px solid var(--border-strong)",
          borderRadius: 10, boxShadow: "var(--shadow-lg)", padding: "8px 11px",
          fontSize: 12.5, lineHeight: 1.45, color: "var(--text-secondary)",
          whiteSpace: "normal", pointerEvents: "none",
        }}>
          {text}
        </div>,
        document.body,
      )}
    </>
  );
}
