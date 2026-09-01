/**
 * Thin fetch wrapper. Reads the JWT from localStorage and attaches it as
 * Bearer. Throws ApiError on non-2xx so React Query can hand it to error UI.
 */

export class ApiError extends Error {
  status: number;
  detail?: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

const TOKEN_KEY = "irs_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(t: string | null) {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}

type ApiOpts = Omit<RequestInit, "body"> & {
  body?: unknown;
  formBody?: Record<string, string>; // for /auth/token which wants form-urlencoded
};

export async function api<T = unknown>(path: string, opts: ApiOpts = {}): Promise<T> {
  const headers = new Headers(opts.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let body: BodyInit | undefined;
  if (opts.formBody) {
    headers.set("Content-Type", "application/x-www-form-urlencoded");
    body = new URLSearchParams(opts.formBody).toString();
  } else if (opts.body instanceof FormData) {
    body = opts.body;
  } else if (opts.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(opts.body);
  }

  const resp = await fetch(path, { ...opts, headers, body });

  if (resp.status === 401) {
    setToken(null);
    // Force a re-render of the auth gate via location change.
    if (!location.pathname.startsWith("/app/login")) {
      location.assign("/app/login");
    }
  }

  const text = await resp.text();
  let parsed: unknown = undefined;
  if (text) {
    try { parsed = JSON.parse(text); } catch { parsed = text; }
  }
  if (!resp.ok) {
    const detail = (parsed as any)?.detail ?? parsed;
    throw new ApiError(resp.status, `${resp.status} ${resp.statusText}`, detail);
  }
  return parsed as T;
}

/** Multipart upload via XHR so callers get byte-level progress, which
 *  fetch() doesn't expose. Same auth + error semantics as `api()`. */
export function apiUpload<T = unknown>(
  path: string,
  fd: FormData,
  onProgress?: (sent: number, total: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    const token = getToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      };
    }
    xhr.onerror = () => reject(new ApiError(0, "Network error"));
    xhr.onload = () => {
      let parsed: unknown;
      try { parsed = xhr.responseText ? JSON.parse(xhr.responseText) : undefined; }
      catch { parsed = xhr.responseText; }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(parsed as T);
      } else {
        const detail = (parsed as any)?.detail ?? parsed;
        reject(new ApiError(xhr.status, `${xhr.status} ${xhr.statusText}`, detail));
      }
    };
    xhr.send(fd);
  });
}
