"use client";
import { useCallback, useEffect, useState } from "react";

export interface ColPrefs {
  /** Every known column key, in display order. */
  order: string[];
  /** Keys the user has switched off. */
  hidden: string[];
}

/**
 * Per-table column order + visibility, persisted in localStorage. Saved prefs
 * are reconciled against the current column list on load, so a column added
 * in a later build appears (at its default position, with its default
 * visibility) instead of vanishing, and a removed one is dropped.
 */
export function useColumnPrefs(storageKey: string, defaultOrder: string[], defaultHidden: string[] = []) {
  const [prefs, setPrefs] = useState<ColPrefs>({ order: defaultOrder, hidden: defaultHidden });

  useEffect(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (!raw) return;
      const saved = JSON.parse(raw) as Partial<ColPrefs>;
      const known = new Set(defaultOrder);
      const order = (saved.order ?? []).filter((k) => known.has(k));
      const hidden = new Set((saved.hidden ?? []).filter((k) => known.has(k)));
      defaultOrder.forEach((k, i) => {
        if (order.includes(k)) return;
        // new column: slot it after its default predecessor, keep its default visibility
        const prev = defaultOrder[i - 1];
        const at = prev ? order.indexOf(prev) + 1 : 0;
        order.splice(at > 0 ? at : order.length, 0, k);
        if (defaultHidden.includes(k)) hidden.add(k);
      });
      setPrefs({ order, hidden: [...hidden] });
    } catch {
      /* corrupt or unavailable storage: keep defaults */
    }
    // defaults are module constants; only the key matters
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey]);

  const persist = useCallback((p: ColPrefs) => {
    setPrefs(p);
    try { localStorage.setItem(storageKey, JSON.stringify(p)); } catch { /* private mode */ }
  }, [storageKey]);

  const toggle = useCallback((key: string) => {
    persist({
      ...prefs,
      hidden: prefs.hidden.includes(key) ? prefs.hidden.filter((k) => k !== key) : [...prefs.hidden, key],
    });
  }, [prefs, persist]);

  const move = useCallback((key: string, dir: -1 | 1) => {
    const i = prefs.order.indexOf(key);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= prefs.order.length) return;
    const order = [...prefs.order];
    [order[i], order[j]] = [order[j], order[i]];
    persist({ ...prefs, order });
  }, [prefs, persist]);

  /** Drop `key` directly before or after `targetKey` (drag-and-drop). */
  const reorder = useCallback((key: string, targetKey: string, place: "before" | "after") => {
    if (key === targetKey) return;
    const order = prefs.order.filter((k) => k !== key);
    const at = order.indexOf(targetKey);
    if (at < 0) return;
    order.splice(place === "after" ? at + 1 : at, 0, key);
    persist({ ...prefs, order });
  }, [prefs, persist]);

  const reset = useCallback(() => {
    try { localStorage.removeItem(storageKey); } catch { /* ignore */ }
    setPrefs({ order: defaultOrder, hidden: defaultHidden });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey]);

  return { prefs, toggle, move, reorder, reset };
}
