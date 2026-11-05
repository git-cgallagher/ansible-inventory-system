import type { Session } from "./types";

const STORAGE_KEY = "ais.session"; // also written by the OIDC callback page (ais/oidc.py)

export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function saveSession(session: Session | null): void {
  try {
    if (session) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage blocked: the session lives only in memory for this page */
  }
}

/** A non-2xx answer. `detail` is FastAPI's `detail` (string, object or list). */
export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(describe(status, detail));
    this.status = status;
    this.detail = detail;
  }
  /** 409 from the If-Match check (as opposed to an exporter refusal). */
  get stale(): { expected: number; current: number } | null {
    const d = this.detail as { error?: string; expected?: number; current?: number } | null;
    return this.status === 409 && d && d.error === "stale revision" && typeof d.current === "number"
      ? { expected: d.expected ?? -1, current: d.current }
      : null;
  }
}

function describe(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg ?? ""}`)
      .join("; ");
  }
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    return typeof message === "string" ? message : JSON.stringify(detail);
  }
  return `HTTP ${status}`;
}

let session: Session | null = loadSession();
let onUnauthorized: () => void = () => {};

export function currentSession(): Session | null {
  return session;
}

export function setSession(next: Session | null): void {
  session = next;
  saveSession(next);
}

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Revision the change is based on; sent as `If-Match: "<n>"`. */
  ifMatch?: number;
  query?: Record<string, string | number | boolean | null | undefined>;
}

export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = new URL(path, window.location.origin);
  for (const [k, v] of Object.entries(opts.query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const headers: Record<string, string> = { Accept: "application/json", "X-AIS-Source": "ui" };
  if (session) headers.Authorization = `Bearer ${session.token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.ifMatch !== undefined) headers["If-Match"] = `"${opts.ifMatch}"`;
  const resp = await fetch(url, {
    method: opts.method ?? "GET",
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  const text = await resp.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!resp.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
    if (resp.status === 401 && !path.endsWith("/auth/login")) {
      setSession(null);
      onUnauthorized();
    }
    throw new ApiError(resp.status, detail);
  }
  return data as T;
}

export function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.message} (HTTP ${err.status})`;
  if (err instanceof Error) return err.message;
  return String(err);
}
