// Thin fetch wrapper. The session cookie is HttpOnly (JS never sees it); the CSRF token lives only
// in memory and is re-fetched from /auth/session after a page reload.

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

let csrf = null;
export const setCsrf = (token) => { csrf = token; };
const listeners = new Set();
export const onUnauthorized = (fn) => listeners.add(fn);

export async function api(method, path, { json, body, headers = {}, query, quiet401 = false } = {}) {
  const qs = query ? "?" + new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined && v !== null && v !== "")) : "";
  const opts = { method, credentials: "same-origin", headers: { ...headers } };
  if (method !== "GET" && csrf) opts.headers["x-csrf-token"] = csrf;
  if (json !== undefined) {
    opts.body = JSON.stringify(json);
    opts.headers["content-type"] = "application/json";
  } else if (body !== undefined) {
    opts.body = body;
  }
  let res;
  try {
    res = await fetch(`/api/v1${path}${qs}`, opts);
  } catch {
    throw new ApiError(0, "network", "Cannot reach the server. Check your connection.");
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error body */ }
  if (!res.ok) {
    const e = data?.error || {};
    const err = new ApiError(res.status, e.code || "http_error", e.message || `Request failed (${res.status})`, e.details);
    if (res.status === 401 && !quiet401 && !path.startsWith("/auth/login")) listeners.forEach((fn) => fn());
    throw err;
  }
  return data;
}

export const filesUrl = (id, path) => `/api/v1/workspaces/${encodeURIComponent(id)}/files/content?path=${encodeURIComponent(path)}`;
