"use client";
import { useEffect } from "react";

import { CurrencyProvider } from "@/lib/ui";

export function Providers({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    const saved = localStorage.getItem("taoscope-theme") || "light";
    document.documentElement.setAttribute("data-theme", saved);
  }, []);
  return <CurrencyProvider>{children}</CurrencyProvider>;
}
