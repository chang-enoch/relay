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
  constructor(public status: number, message: string) {
    super(message);
  }
}

// FastAPI errors: our own { message }, HTTPException's { detail: "..." }, or validation's { detail: [{ msg }] }.
function errorMessage(data: { message?: string; detail?: string | { msg?: string }[] }): string {
  if (data.message) return data.message;
  if (typeof data.detail === "string") return data.detail;
  return data.detail?.[0]?.msg ?? "Request failed";
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
    throw new ApiError(res.status, errorMessage(data));
  }
  return data as T;
}
