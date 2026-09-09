"use client";
import { useEffect, useRef, useState } from "react";

export type Subnet = Record<string, any>;

export interface LiveState {
  subnets: Subnet[];
  block: number;
  taoUsd: number;
  connected: boolean;
  lastTick: number;
}

/** Single websocket to the backend, auto-reconnecting with backoff. */
export function useLive(): LiveState {
  const [state, setState] = useState<LiveState>({
    subnets: [], block: 0, taoUsd: 0, connected: false, lastTick: 0,
  });
  const wsRef = useRef<WebSocket | null>(null);
  const retry = useRef(0);
  const dead = useRef(false);

  useEffect(() => {
    dead.current = false;

    const connect = () => {
      if (dead.current) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      wsRef.current = ws;

      ws.onopen = () => {
        retry.current = 0;
        setState((s) => ({ ...s, connected: true }));
      };
      ws.onmessage = (ev) => {
        try {
          const { event, data } = JSON.parse(ev.data);
          if (event === "subnets") {
            setState((s) => ({
              ...s,
              subnets: data.subnets ?? s.subnets,
              block: data.block ?? s.block,
              taoUsd: data.tao_usd || s.taoUsd,
              connected: true,
              lastTick: Date.now(),
            }));
          }
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onclose = () => {
        setState((s) => ({ ...s, connected: false }));
        if (dead.current) return;
        retry.current = Math.min(retry.current + 1, 6);
        setTimeout(connect, 1000 * 2 ** (retry.current - 1));
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      dead.current = true;
      wsRef.current?.close();
    };
  }, []);

  return state;
}
