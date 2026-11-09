export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export const WS_URL = API_URL.replace(/^http/, "ws");

export interface Creds {
  session_id: string;
  participant_id: string;
  token: string;
  room_code: string;
}

const KEY = "relay.creds";

export function loadCreds(): Creds | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Creds) : null;
  } catch {
    return null;
  }
}

export function saveCreds(c: Creds) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(c));
  } catch {}
}

export function clearCreds() {
  try {
    sessionStorage.removeItem(KEY);
  } catch {}
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
  }
}

export async function api<T>(path: string, opts: { method?: string; body?: unknown; token?: string } = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: opts.method ?? (opts.body ? "POST" : "GET"),
    headers: {
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
      ...(opts.token ? { Authorization: `Bearer ${opts.token}` } : {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.message ?? (typeof data.detail === "string" ? data.detail : "Request failed");
    throw new ApiError(res.status, msg, data.code);
  }
  return data as T;
}
