// Thin client for the FastAPI backend: one function per endpoint, no business logic.
// The session lives in an HttpOnly cookie the script can't read; state-changing
// requests carry the CSRF token from /api/auth/me in the X-CSRF-Token header.
import { buildQuery, formatApiError } from "./utils.js";

export class ApiError extends Error {
  constructor(status, message, body = null, retryAfter = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.retryAfter = retryAfter;
  }
}

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
let csrfToken = null;
let unauthorizedHandler = null;

export function setCsrfToken(token) {
  csrfToken = token;
}

/** Called with the server's message whenever a request comes back 401 (signed out or session expired). */
export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
}

const enc = encodeURIComponent;

/**
 * Returns {status, data}; throws ApiError (status 0 = network failure). Aborts are rethrown as-is.
 * A CSRF failure (e.g. the server restarted) refreshes the token once and retries.
 */
export async function request(method, path, { query, body, signal, authRedirect = true, retryCsrf = true } = {}) {
  const init = { method, signal, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (UNSAFE_METHODS.has(method) && csrfToken) init.headers["X-CSRF-Token"] = csrfToken;
  let response;
  try {
    response = await fetch(path + buildQuery(query), init);
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    throw new ApiError(0, formatApiError(0, null));
  }
  let data = null;
  const text = await response.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    if (response.status === 403 && retryCsrf && response.headers.get("X-Error-Code") === "csrf_failed") {
      const me = await request("GET", "/api/auth/me", { retryCsrf: false });
      setCsrfToken(me.data.csrf_token);
      return request(method, path, { query, body, signal, authRedirect, retryCsrf: false });
    }
    const message = formatApiError(response.status, data);
    if (response.status === 401 && authRedirect && unauthorizedHandler) unauthorizedHandler(message);
    const retryAfter = Number(response.headers.get("Retry-After")) || null;
    throw new ApiError(response.status, message, data, retryAfter);
  }
  return { status: response.status, data };
}

export const api = {
  health: () => request("GET", "/api/health"),
  login: (email, password) => request("POST", "/api/auth/login", { body: { email, password }, authRedirect: false }),
  logout: () => request("POST", "/api/auth/logout", { authRedirect: false }),
  me: () => request("GET", "/api/auth/me"),
  changePassword: (currentPassword, newPassword) =>
    request("POST", "/api/auth/password", { body: { current_password: currentPassword, new_password: newPassword } }),
  facets: () => request("GET", "/api/facets"),
  listCandidates: (params, signal) => request("GET", "/api/candidates", { query: params, signal }),
  getCandidate: (id, signal) => request("GET", `/api/candidates/${enc(id)}`, { signal }),
  rank: (params, signal) => request("GET", "/api/rank", { query: params, signal }),
  addTag: (id, tag) => request("POST", `/api/candidates/${enc(id)}/tags`, { body: { tag } }),
  removeTag: (id, tag) => request("DELETE", `/api/candidates/${enc(id)}/tags/${enc(tag)}`),
  setShortlisted: (id, shortlisted) =>
    request("PUT", `/api/candidates/${enc(id)}/shortlist`, { body: { shortlisted } }),
  cachedSummary: (id, requirements = []) =>
    request("GET", `/api/candidates/${enc(id)}/summary`, { query: { requirements } }),
  summary: (id, { requirements = [], refresh = false } = {}) =>
    request("POST", `/api/candidates/${enc(id)}/summary`, { body: { requirements, refresh } }),
  activity: (params) => request("GET", "/api/activity", { query: params }),
  listUsers: () => request("GET", "/api/admin/users"),
  createUser: (user) => request("POST", "/api/admin/users", { body: user }),
  updateUser: (id, changes) => request("PATCH", `/api/admin/users/${enc(id)}`, { body: changes }),
  resetPassword: (id, password) => request("POST", `/api/admin/users/${enc(id)}/password`, { body: { password } }),
  auditLogs: (params) => request("GET", "/api/admin/audit-logs", { query: params }),
};
