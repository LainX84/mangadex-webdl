export class ApiError extends Error {
  status: number;
  retryAfter?: number;
  constructor(status: number, message: string, retryAfter?: number) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

function readCookie(name: string): string | null {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : null;
}

let csrfToken: string | null = null;
export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

type Unauthorized = () => void;
let onUnauthorized: Unauthorized = () => {};
export function setUnauthorizedHandler(fn: Unauthorized) {
  onUnauthorized = fn;
}

export async function api<T = unknown>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") {
    headers["X-CSRF-Token"] = csrfToken ?? readCookie("mdm_csrf") ?? "";
  }
  let body: string | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const res = await fetch(path, { method, headers, body, credentials: "same-origin" });
  if (res.status === 401 && !path.startsWith("/api/auth/login") && !path.startsWith("/api/auth/setup")) {
    onUnauthorized();
  }
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") detail = data.detail;
      else if (Array.isArray(data?.detail)) detail = data.detail.map((d: { msg?: string }) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    const ra = res.headers.get("Retry-After");
    throw new ApiError(res.status, detail, ra ? Number(ra) : undefined);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const post = <T = unknown>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: body ?? {} });
export const put = <T = unknown>(path: string, body: unknown) => api<T>(path, { method: "PUT", body });
export const patch = <T = unknown>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body });
export const del = <T = unknown>(path: string, body?: unknown) => api<T>(path, { method: "DELETE", body: body ?? {} });
