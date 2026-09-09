"use client";
import { ReactNode, useMemo, useState } from "react";

export interface Col<T> {
  key: string;
  label: string;
  align?: "left" | "right";
  render?: (row: T) => ReactNode;
  value?: (row: T) => number | string | null;
  width?: number;
  title?: string;
  /** First click direction. Metrics default to descending, names/ids to ascending. */
  defaultDesc?: boolean;
}

export type ReorderPlace = "before" | "after";

export function DataTable<T>({
  rows, cols, initialSort, initialDesc = true, rowKey, onRowClick, maxHeight, tieBreak, onReorder, rowClass,
}: {
  rows: T[];
  cols: Col<T>[];
  initialSort?: string;
  initialDesc?: boolean;
  rowKey: (row: T) => string | number;
  onRowClick?: (row: T) => void;
  maxHeight?: number;
  /** Keeps equal rows in a fixed order so live updates don't make them jitter. */
  tieBreak?: (row: T) => number | string;
  /** When given, headers can be dragged onto each other to reorder columns. */
  onReorder?: (key: string, targetKey: string, place: ReorderPlace) => void;
  /** Extra class for a row (role / ownership tints live in globals.css). */
  rowClass?: (row: T) => string | undefined;
}) {
  const [sort, setSort] = useState(initialSort ?? cols[0].key);
  const [desc, setDesc] = useState(initialDesc);
  const [drag, setDrag] = useState<string | null>(null);
  const [over, setOver] = useState<{ key: string; place: ReorderPlace } | null>(null);

  const placeFor = (e: React.DragEvent<HTMLElement>): ReorderPlace => {
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientX < r.left + r.width / 2 ? "before" : "after";
  };
  const endDrag = () => { setDrag(null); setOver(null); };

  // The sorted column can disappear (the user hid it): fall back to the initial
  // sort, then to the first sortable column, instead of silently unsorting.
  const effectiveSort = useMemo(() => {
    const has = (k?: string) => k !== undefined && cols.some((c) => c.key === k && c.value);
    return has(sort) ? sort : has(initialSort) ? initialSort! : (cols.find((c) => c.value)?.key ?? sort);
  }, [cols, sort, initialSort]);

  const sorted = useMemo(() => {
    const col = cols.find((c) => c.key === effectiveSort);
    if (!col?.value) return rows;
    const dir = desc ? -1 : 1;

    return [...rows].sort((a, b) => {
      const av = col.value!(a);
      const bv = col.value!(b);

      // Missing values always sink to the bottom, in BOTH directions. Reversing a
      // sorted array (the old approach) floated them to the top on descending.
      const aMissing = av === null || av === undefined || (typeof av === "number" && Number.isNaN(av));
      const bMissing = bv === null || bv === undefined || (typeof bv === "number" && Number.isNaN(bv));
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1;
      if (bMissing) return -1;

      let cmp: number;
      if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
      else cmp = String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: "base" });

      if (cmp !== 0) return cmp * dir;

      if (tieBreak) {
        const at = tieBreak(a), bt = tieBreak(b);
        if (typeof at === "number" && typeof bt === "number") return at - bt;
        return String(at).localeCompare(String(bt));
      }
      return 0;
    });
  }, [rows, effectiveSort, desc, cols, tieBreak]);

  const click = (c: Col<T>) => {
    if (!c.value) return;
    if (c.key === effectiveSort) setDesc((d) => !d);
    else {
      setSort(c.key);
      setDesc(c.defaultDesc ?? true);
    }
  };

  return (
    <div className="overflow-auto" style={{ maxHeight }}>
      <table className="dense w-full text-[12.5px]">
        <thead>
          <tr>
            {cols.map((c) => {
              const active = effectiveSort === c.key;
              return (
                <th
                  key={c.key}
                  title={c.title}
                  onClick={() => click(c)}
                  draggable={!!onReorder}
                  onDragStart={(e) => {
                    if (!onReorder) return;
                    e.dataTransfer.effectAllowed = "move";
                    e.dataTransfer.setData("text/plain", c.key);
                    setDrag(c.key);
                  }}
                  onDragOver={(e) => {
                    if (!onReorder || !drag || drag === c.key) return;
                    e.preventDefault();
                    e.dataTransfer.dropEffect = "move";
                    const place = placeFor(e);
                    if (over?.key !== c.key || over.place !== place) setOver({ key: c.key, place });
                  }}
                  onDrop={(e) => {
                    if (!onReorder) return;
                    e.preventDefault();
                    const from = drag ?? e.dataTransfer.getData("text/plain");
                    if (from && from !== c.key) onReorder(from, c.key, placeFor(e));
                    endDrag();
                  }}
                  onDragEnd={endDrag}
                  className={`px-3.5 ${c.value ? "cursor-pointer select-none group" : ""}`}
                  style={{
                    textAlign: c.align ?? "left",
                    width: c.width,
                    color: active ? "var(--text-primary)" : undefined,
                    opacity: drag === c.key ? 0.4 : undefined,
                    boxShadow: over?.key === c.key
                      ? (over.place === "before" ? "inset 3px 0 0 var(--accent)" : "inset -3px 0 0 var(--accent)")
                      : undefined,
                  }}
                >
                  <span className="inline-flex items-center gap-1"
                        style={{ flexDirection: c.align === "right" ? "row-reverse" : "row" }}>
                    {c.label}
                    {c.value && (
                      <span
                        aria-hidden
                        className={active ? "" : "opacity-0 group-hover:opacity-40 transition-opacity"}
                        style={{ color: active ? "var(--accent)" : "var(--text-muted)", fontSize: 10 }}
                      >
                        {active ? (desc ? "▼" : "▲") : "▼"}
                      </span>
                    )}
                  </span>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={rowKey(r)} onClick={() => onRowClick?.(r)}
                className={[onRowClick ? "cursor-pointer" : "", rowClass?.(r) ?? ""].join(" ").trim()}>
              {cols.map((c) => (
                <td key={c.key} className="px-3.5 py-[9px]"
                    style={{ textAlign: c.align ?? "left" }}>
                  {c.render ? c.render(r) : String((r as any)[c.key] ?? "—")}
                </td>
              ))}
            </tr>
          ))}
          {sorted.length === 0 && (
            <tr>
              <td colSpan={cols.length} className="px-3.5 py-10 text-center"
                  style={{ color: "var(--text-muted)" }}>
                nothing matches those filters
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
