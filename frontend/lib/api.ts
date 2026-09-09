const BASE = process.env.NEXT_PUBLIC_API_BASE || "";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "content-type": "application/json",
      // Required by the server on every mutation; a cross-site page cannot set it.
      "x-requested-with": "taoscope",
      ...(init?.headers || {}),
    },
  });
  if (res.status === 401) {
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      window.location.href = "/login";
    }
    throw new ApiError(401, "unauthenticated");
  }
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.status === 204 ? (null as T) : res.json();
}

export const post = <T = any>(p: string, body?: unknown) =>
  api<T>(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const put = <T = any>(p: string, body: unknown) =>
  api<T>(p, { method: "PUT", body: JSON.stringify(body) });

export const del = <T = any>(p: string) => api<T>(p, { method: "DELETE" });
