// Thin client for the FastAPI backend. An optional bearer token (AI_FACTORY_TOKEN on the
// server) is kept in localStorage and sent with every call.

const TOKEN_KEY = "ai-factory.token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* storage unavailable: token lasts for this page only */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { ...init, headers: { ...headers, ...(init.headers as object) } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const patch = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(body) });

export const del = (path: string) => api<void>(path, { method: "DELETE" });

export type ControlAction = "pause" | "resume" | "stop" | "estop" | "clear_estop";

export const control = (
  scope: "global" | "project" | "task",
  action: ControlAction,
  target_id?: number,
) => post<{ ok: boolean }>("/api/controls", { scope, action, target_id });

export function streamUrl(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) q.set(k, String(v));
  const token = getToken();
  if (token) q.set("token", token); // EventSource cannot send headers
  return `/api/stream?${q}`;
}

export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });
