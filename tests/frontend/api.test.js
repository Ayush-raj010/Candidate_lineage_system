import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { ApiError, api, onUnauthorized, request, setCsrfToken } from "../../static/js/api.js";

const realFetch = globalThis.fetch;
let calls = [];

function stubFetch(handler) {
  calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return handler(url, init);
  };
}

function jsonResponse(status, body) {
  return new Response(body === undefined ? "" : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  globalThis.fetch = realFetch;
  setCsrfToken(null);
  onUnauthorized(null);
});

describe("request", () => {
  test("returns status and parsed JSON", async () => {
    stubFetch(() => jsonResponse(201, { id: "cand_001", tags: ["x"] }));
    assert.deepEqual(await request("POST", "/api/x", { body: { tag: "x" } }), { status: 201, data: { id: "cand_001", tags: ["x"] } });
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.headers["Content-Type"], "application/json");
    assert.equal(calls[0].init.body, JSON.stringify({ tag: "x" }));
  });

  test("GET sends no body and builds the query", async () => {
    stubFetch(() => jsonResponse(200, {}));
    await request("GET", "/api/rank", { query: { skills: ["Go", "Rust"], role: "" } });
    assert.equal(calls[0].url, "/api/rank?skills=Go&skills=Rust");
    assert.equal(calls[0].init.body, undefined);
  });

  test("HTTP errors become ApiError with the server's message", async () => {
    stubFetch(() => jsonResponse(404, { detail: "Candidate not found" }));
    await assert.rejects(request("GET", "/api/candidates/x"), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 404);
      assert.equal(error.message, "Candidate not found");
      return true;
    });
  });

  test("error bodies that aren't JSON still give a message", async () => {
    stubFetch(() => new Response("Internal Server Error", { status: 500 }));
    await assert.rejects(request("GET", "/api/x"), { name: "ApiError", status: 500, message: /HTTP 500/ });
  });

  test("network failures become status 0", async () => {
    stubFetch(() => { throw new TypeError("Failed to fetch"); });
    await assert.rejects(request("GET", "/api/x"), { name: "ApiError", status: 0, message: /reach the server/ });
  });

  test("aborts are passed through untouched", async () => {
    stubFetch(() => { throw new DOMException("aborted", "AbortError"); });
    await assert.rejects(request("GET", "/api/x"), { name: "AbortError" });
  });
});

describe("authentication", () => {
  test("state-changing requests carry the CSRF token; reads don't", async () => {
    stubFetch(() => jsonResponse(200, {}));
    setCsrfToken("abc123");
    await api.addTag("cand_001", "x");
    await api.updateUser(5, { role: "recruiter" });
    await api.listCandidates({});
    assert.equal(calls[0].init.headers["X-CSRF-Token"], "abc123");
    assert.equal(calls[1].init.headers["X-CSRF-Token"], "abc123");
    assert.equal(calls[2].init.headers["X-CSRF-Token"], undefined);
    assert.ok(calls.every((c) => c.init.credentials === "same-origin"));
  });

  test("a 401 calls the session-ended handler with the server's message", async () => {
    stubFetch(() => jsonResponse(401, { detail: "Your session has expired. Sign in again." }));
    const seen = [];
    onUnauthorized((message) => seen.push(message));
    await assert.rejects(api.listCandidates({}), { status: 401 });
    assert.deepEqual(seen, ["Your session has expired. Sign in again."]);
  });

  test("a failed login is an error for the form, not a session expiry", async () => {
    stubFetch(() => jsonResponse(401, { detail: "Incorrect email or password." }));
    const seen = [];
    onUnauthorized((message) => seen.push(message));
    await assert.rejects(api.login("a@b.co", "x"), { status: 401, message: "Incorrect email or password." });
    assert.deepEqual(seen, []);
  });

  test("a stale CSRF token is refreshed once and the request retried", async () => {
    setCsrfToken("old");
    let posts = 0;
    stubFetch((url, init) => {
      if (url === "/api/auth/me") return jsonResponse(200, { csrf_token: "new" });
      posts += 1;
      return posts === 1
        ? new Response(JSON.stringify({ detail: "Missing or invalid CSRF token." }), { status: 403, headers: { "X-Error-Code": "csrf_failed" } })
        : jsonResponse(201, { tags: ["x"] });
    });
    const result = await api.addTag("cand_001", "x");
    assert.equal(result.status, 201);
    assert.deepEqual(calls.map((c) => `${c.init.method} ${c.url} ${c.init.headers["X-CSRF-Token"] || "-"}`), [
      "POST /api/candidates/cand_001/tags old",
      "GET /api/auth/me -",
      "POST /api/candidates/cand_001/tags new",
    ]);
  });

  test("a permission 403 is not retried", async () => {
    stubFetch(() => jsonResponse(403, { detail: "Your role (Recruiter) doesn't allow this action." }));
    await assert.rejects(api.listUsers(), { status: 403, message: /Recruiter/ });
    assert.equal(calls.length, 1);
  });

  test("Retry-After is exposed for throttled logins", async () => {
    stubFetch(() => new Response(JSON.stringify({ detail: "Too many" }), { status: 429, headers: { "Retry-After": "120" } }));
    await assert.rejects(api.login("a@b.co", "x"), (error) => error.retryAfter === 120);
  });
});

describe("endpoints", () => {
  test("every call hits the documented method and path", async () => {
    stubFetch(() => jsonResponse(200, {}));
    await api.health();
    await api.login("a@b.co", "pw");
    await api.logout();
    await api.me();
    await api.changePassword("old", "new");
    await api.facets();
    await api.listCandidates({ q: "go", limit: 20, offset: 0 });
    await api.getCandidate("cand_001");
    await api.rank({ skills: ["Python"] });
    await api.addTag("cand_001", "strong backend");
    await api.removeTag("cand_001", "strong backend");
    await api.setShortlisted("cand_001", true);
    await api.cachedSummary("cand_001", ["Java", "Go"]);
    await api.summary("cand_001", { requirements: ["Java"] });
    await api.activity({ limit: 25, offset: 0 });
    await api.listUsers();
    await api.createUser({ email: "x@y.co", role: "recruiter", password: "p" });
    await api.updateUser(7, { is_active: false });
    await api.resetPassword(7, "new password");
    await api.auditLogs({ limit: 25, offset: 25 });
    assert.deepEqual(calls.map((c) => `${c.init.method} ${c.url}`), [
      "GET /api/health",
      "POST /api/auth/login",
      "POST /api/auth/logout",
      "GET /api/auth/me",
      "POST /api/auth/password",
      "GET /api/facets",
      "GET /api/candidates?q=go&limit=20&offset=0",
      "GET /api/candidates/cand_001",
      "GET /api/rank?skills=Python",
      "POST /api/candidates/cand_001/tags",
      "DELETE /api/candidates/cand_001/tags/strong%20backend",
      "PUT /api/candidates/cand_001/shortlist",
      "GET /api/candidates/cand_001/summary?requirements=Java&requirements=Go",
      "POST /api/candidates/cand_001/summary",
      "GET /api/activity?limit=25&offset=0",
      "GET /api/admin/users",
      "POST /api/admin/users",
      "PATCH /api/admin/users/7",
      "POST /api/admin/users/7/password",
      "GET /api/admin/audit-logs?limit=25&offset=25",
    ]);
    const body = (i) => JSON.parse(calls[i].init.body);
    assert.deepEqual(body(1), { email: "a@b.co", password: "pw" });
    assert.deepEqual(body(4), { current_password: "old", new_password: "new" });
    assert.deepEqual(body(11), { shortlisted: true });
    assert.deepEqual(body(13), { requirements: ["Java"], refresh: false });
    assert.deepEqual(body(17), { is_active: false });
    assert.deepEqual(body(18), { password: "new password" });
  });

  test("ids and tags are URL-encoded", async () => {
    stubFetch(() => jsonResponse(200, {}));
    await api.getCandidate("a/b c");
    await api.removeTag("x", "a/b");
    assert.equal(calls[0].url, "/api/candidates/a%2Fb%20c");
    assert.equal(calls[1].url, "/api/candidates/x/tags/a%2Fb");
  });
});
