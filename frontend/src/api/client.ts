/**
 * Single fetch wrapper for the backend API.
 * In dev the Vite proxy forwards /api and /health to the FastAPI server;
 * set VITE_API_BASE_URL to target a remote deployment.
 */

const API_BASE: string = import.meta.env.VITE_API_BASE_URL ?? "";

export const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** Backend stub endpoints respond 501 until implemented. */
  get isNotImplemented(): boolean {
    return this.status === 501;
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError("Cannot reach the API server. Is the backend running?", 0);
  }

  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    let detail: unknown;
    try {
      const body = await res.json();
      detail = body.detail ?? body.error;
      if (typeof detail === "string") message = detail;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new ApiError(message, res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const get = <T>(path: string) => request<T>(path);

export const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

export const put = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });

export const del = <T>(path: string) => request<T>(path, { method: "DELETE" });
