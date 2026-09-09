"use client";
import { useEffect, useRef, useState } from "react";

import type { ColPrefs } from "@/lib/columns";

/**
 * "Columns" button + popover: tick columns on and off, reorder by dragging a
 * row (or with the arrows), reset to the default layout. Locked columns can be
 * moved but not hidden. The table headers can be dragged too.
 */
export function ColumnPicker({ prefs, labels, locked = [], onToggle, onMove, onReorder, onReset }: {
  prefs: ColPrefs;
  labels: Record<string, string>;
  locked?: string[];
  onToggle: (key: string) => void;
  onMove: (key: string, dir: -1 | 1) => void;
  onReorder: (key: string, targetKey: string, place: "before" | "after") => void;
  onReset: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [drag, setDrag] = useState<string | null>(null);
  const [over, setOver] = useState<{ key: string; place: "before" | "after" } | null>(null);
  const box = useRef<HTMLDivElement>(null);

  const placeFor = (e: React.DragEvent<HTMLElement>): "before" | "after" => {
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientY < r.top + r.height / 2 ? "before" : "after";
  };
  const endDrag = () => { setDrag(null); setOver(null); };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const shown = prefs.order.length - prefs.hidden.length;

  return (
    <div ref={box} className="relative">
      <button onClick={() => setOpen((v) => !v)} className="btn btn-ghost flex items-center gap-1.5"
              title="Choose and order the table's columns">
        Columns
        <span className="tnum text-[11px]" style={{ color: "var(--text-muted)" }}>{shown}/{prefs.order.length}</span>
      </button>

      {open && (
        <div className="card absolute left-0 mt-1.5 p-2 w-[268px]"
             style={{ zIndex: 60, boxShadow: "var(--shadow-lg)" }}>
          <div className="flex items-center justify-between px-1.5 pb-1.5 mb-1 border-b"
               style={{ borderColor: "var(--border)" }}>
            <span className="text-[12px] font-medium">Columns</span>
            <button className="text-[11.5px] hover:underline" style={{ color: "var(--text-muted)" }}
                    onClick={onReset}>reset</button>
          </div>
          <div className="max-h-[420px] overflow-y-auto">
            {prefs.order.map((key, i) => {
              const isLocked = locked.includes(key);
              const on = !prefs.hidden.includes(key);
              return (
                <div key={key} className="flex items-center gap-2 px-1.5 py-[3px] rounded-md text-[12.5px] cursor-grab"
                     draggable
                     onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", key); setDrag(key); }}
                     onDragOver={(e) => {
                       if (!drag || drag === key) return;
                       e.preventDefault();
                       e.dataTransfer.dropEffect = "move";
                       const place = placeFor(e);
                       if (over?.key !== key || over.place !== place) setOver({ key, place });
                     }}
                     onDrop={(e) => {
                       e.preventDefault();
                       const from = drag ?? e.dataTransfer.getData("text/plain");
                       if (from && from !== key) onReorder(from, key, placeFor(e));
                       endDrag();
                     }}
                     onDragEnd={endDrag}
                     style={{
                       color: on ? "var(--text-primary)" : "var(--text-muted)",
                       opacity: drag === key ? 0.4 : undefined,
                       boxShadow: over?.key === key
                         ? (over.place === "before" ? "inset 0 2px 0 var(--accent)" : "inset 0 -2px 0 var(--accent)")
                         : undefined,
                     }}>
                  <span aria-hidden className="select-none text-[11px] tracking-[-2px]" style={{ color: "var(--text-muted)" }}>⋮⋮</span>
                  <input type="checkbox" checked={on} disabled={isLocked}
                         onChange={() => onToggle(key)} title={isLocked ? "Always shown" : undefined} />
                  <span className="flex-1 truncate">{labels[key] ?? key}</span>
                  <button className="px-1 opacity-50 hover:opacity-100 disabled:opacity-15"
                          disabled={i === 0} onClick={() => onMove(key, -1)} title="Move left">▲</button>
                  <button className="px-1 opacity-50 hover:opacity-100 disabled:opacity-15"
                          disabled={i === prefs.order.length - 1} onClick={() => onMove(key, 1)} title="Move right">▼</button>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
