import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  BROWSE_DEFAULTS, addUnique, buildQuery, coveragePercent, describeAction, fitLabel, formatApiError, formatDate,
  formatDateTime, formatYears, pageInfo, readUrlState, safeNextPath, splitSkillInput, visibleSkills, writeUrlState,
} from "../../static/js/utils.js";

describe("safeNextPath", () => {
  test("keeps same-site paths", () => {
    assert.equal(safeNextPath("/"), "/");
    assert.equal(safeNextPath("/?view=rank&skills=Go"), "/?view=rank&skills=Go");
  });
  test("refuses anything that could leave the site or loop", () => {
    for (const bad of ["https://evil.example", "//evil.example", "/\\evil.example", "evil", "", null, undefined,
      "javascript:alert(1)", "/login", "/login?next=/", "/\nfoo"]) {
      assert.equal(safeNextPath(bad), "/", String(bad));
    }
  });
});

describe("describeAction", () => {
  test("candidate actions", () => {
    assert.equal(describeAction({ action: "tag_added", target_type: "candidate", target_id: "cand_9", candidate_name: "Siddharth Mehta", details: { tag: "follow-up" } }),
      "added tag “follow-up” Siddharth Mehta");
    assert.equal(describeAction({ action: "shortlist_added", target_type: "candidate", target_id: "cand_9", candidate_name: null, details: null }),
      "shortlisted cand_9");
    assert.equal(describeAction({ action: "summary_generated", target_type: "candidate", target_id: "c", candidate_name: "A", details: { fit: "weak" } }),
      "generated an AI summary for A (Weak fit)");
  });
  test("account actions", () => {
    assert.equal(describeAction({ action: "user_role_changed", target_type: "user", target_id: "3", details: { email: "x@y.co", from: "recruiter", to: "admin" } }),
      "changed role of x@y.co (Recruiter → Admin)");
    assert.equal(describeAction({ action: "user_created", target_type: "user", target_id: "3", details: { email: "x@y.co", role: "hiring_manager" } }),
      "created user x@y.co as Hiring manager");
    assert.equal(describeAction({ action: "something_new", target_type: null, details: null }), "something new");
  });
});

describe("buildQuery", () => {
  test("skips empty values and repeats arrays", () => {
    assert.equal(buildQuery({ q: "", role: "ML Engineer", skills: ["Python", "Next.js"], offset: 0, x: null }),
      "?role=ML+Engineer&skills=Python&skills=Next.js&offset=0");
  });
  test("returns an empty string when nothing is set", () => {
    assert.equal(buildQuery({ q: "", tag: undefined, skills: [] }), "");
    assert.equal(buildQuery(), "");
  });
  test("encodes special characters", () => {
    assert.equal(buildQuery({ q: "100% & co", skills: ["CI/CD"] }), "?q=100%25+%26+co&skills=CI%2FCD");
  });
  test("keeps booleans and zero", () => {
    assert.equal(buildQuery({ shortlisted: false, limit: 0 }), "?shortlisted=false&limit=0");
  });
});

describe("formatting", () => {
  test("years", () => {
    assert.equal(formatYears(1), "1 yr");
    assert.equal(formatYears(6), "6 yrs");
    assert.equal(formatYears(0), "0 yrs");
    assert.equal(formatYears(null), "Not stated");
    assert.equal(formatYears(undefined), "Not stated");
  });
  test("dates are not shifted by time zone", () => {
    assert.equal(formatDate("2026-09-01"), "1 Sep 2026");
    assert.equal(formatDate("2026-12-31"), "31 Dec 2026");
    assert.equal(formatDate(null), "Not stated");
    assert.equal(formatDate("garbage"), "garbage");
  });
  test("timestamps", () => {
    assert.equal(formatDateTime("2026-10-26T14:01:05+00:00"), "26 Oct 2026, 14:01 UTC");
    assert.equal(formatDateTime(""), "");
  });
  test("fit labels cover every API value", () => {
    assert.equal(fitLabel("strong"), "Strong fit");
    assert.equal(fitLabel("insufficient_evidence"), "Insufficient evidence");
    assert.equal(fitLabel("unexpected"), "unexpected");
  });
  test("coverage percent is clamped", () => {
    assert.equal(coveragePercent(0.3333), 33);
    assert.equal(coveragePercent(1), 100);
    assert.equal(coveragePercent(2), 100);
    assert.equal(coveragePercent(undefined), 0);
  });
});

describe("pageInfo", () => {
  test("first, middle and last pages", () => {
    assert.deepEqual(pageInfo(48, 20, 0), { label: "1–20 of 48", hasPrev: false, hasNext: true, page: 1, pages: 3 });
    assert.deepEqual(pageInfo(48, 20, 20), { label: "21–40 of 48", hasPrev: true, hasNext: true, page: 2, pages: 3 });
    assert.deepEqual(pageInfo(48, 20, 40), { label: "41–48 of 48", hasPrev: true, hasNext: false, page: 3, pages: 3 });
  });
  test("exact fit has no next page", () => {
    assert.equal(pageInfo(40, 20, 20).hasNext, false);
  });
  test("empty and out-of-range", () => {
    assert.equal(pageInfo(0, 20, 0).label, "No results");
    const past = pageInfo(48, 20, 100);
    assert.equal(past.hasPrev, true);
    assert.equal(past.hasNext, false);
    assert.match(past.label, /48 total/);
  });
});

describe("formatApiError", () => {
  test("uses a string detail as-is", () => {
    assert.equal(formatApiError(404, { detail: "Candidate not found" }), "Candidate not found");
  });
  test("turns FastAPI validation errors into field messages", () => {
    const body = { detail: [
      { loc: ["body", "tag"], msg: "Value error, tag must be 1-40 characters" },
      { loc: ["query", "limit"], msg: "Input should be less than or equal to 200" },
    ] };
    assert.equal(formatApiError(422, body), "tag: tag must be 1-40 characters; limit: Input should be less than or equal to 200");
  });
  test("falls back by status", () => {
    assert.match(formatApiError(0, null), /reach the server/);
    assert.equal(formatApiError(404, null), "Not found.");
    assert.match(formatApiError(502, {}), /HTTP 502/);
    assert.match(formatApiError(418, "not json"), /HTTP 418/);
  });
});

describe("skill input", () => {
  test("splits on commas and tidies spaces", () => {
    assert.deepEqual(splitSkillInput(" Python,  Next.js ,, REST   APIs "), ["Python", "Next.js", "REST APIs"]);
    assert.deepEqual(splitSkillInput(""), []);
  });
  test("addUnique ignores case and keeps order", () => {
    assert.deepEqual(addUnique(["Python"], ["python", "Go", "GO", "Rust"]), ["Python", "Go", "Rust"]);
  });
  test("visibleSkills leaves room for a +N chip", () => {
    assert.deepEqual(visibleSkills(["a", "b", "c"], 4), { shown: ["a", "b", "c"], hidden: [] });
    assert.deepEqual(visibleSkills(["a", "b", "c", "d"], 4), { shown: ["a", "b", "c", "d"], hidden: [] });
    assert.deepEqual(visibleSkills(["a", "b", "c", "d", "e"], 4), { shown: ["a", "b", "c"], hidden: ["d", "e"] });
  });
});

describe("URL state", () => {
  test("defaults when the URL is empty", () => {
    const state = readUrlState("");
    assert.equal(state.view, "browse");
    assert.deepEqual(state.browse, BROWSE_DEFAULTS);
    assert.deepEqual(state.rank.skills, []);
  });
  test("browse filters round-trip", () => {
    const browse = { ...BROWSE_DEFAULTS, q: "kubernetes", role: "ML Engineer", shortlisted: "true", sort: "name", limit: 50, offset: 50 };
    const query = writeUrlState("browse", browse, {});
    assert.equal(query, "?q=kubernetes&role=ML+Engineer&shortlisted=true&sort=name&limit=50&offset=50");
    assert.deepEqual(readUrlState(query).browse, browse);
  });
  test("default browse state gives a clean URL", () => {
    assert.equal(writeUrlState("browse", { ...BROWSE_DEFAULTS }, {}), "");
  });
  test("rank state round-trips", () => {
    const rank = { skills: ["Python", "CI/CD"], role: "", source: "AngelList", limit: 20, offset: 20 };
    const query = writeUrlState("rank", {}, rank);
    const state = readUrlState(query);
    assert.equal(state.view, "rank");
    assert.deepEqual(state.rank, rank);
  });
  test("invalid values fall back to defaults", () => {
    const state = readUrlState("?sort=random()&limit=7&offset=-3&shortlisted=maybe");
    assert.equal(state.browse.sort, "-applied_date");
    assert.equal(state.browse.limit, 20);
    assert.equal(state.browse.offset, 0);
    assert.equal(state.browse.shortlisted, "");
  });
  test("activity and users views round-trip; unknown views fall back to browse", () => {
    assert.equal(readUrlState(writeUrlState("activity", BROWSE_DEFAULTS, {})).view, "activity");
    assert.equal(readUrlState(writeUrlState("users", BROWSE_DEFAULTS, {})).view, "users");
    assert.equal(readUrlState("?view=admin").view, "browse");
  });
  test("rank skills from the URL are split and de-duplicated", () => {
    assert.deepEqual(readUrlState("?view=rank&skills=Python,Go&skills=python").rank.skills, ["Python", "Go"]);
  });
});
