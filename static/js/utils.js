// Pure helpers for the dashboard. No DOM access, so Node's test runner can run them
// (tests/frontend/). Option lists mirror values the API accepts; tests/test_frontend.py
// checks them against the backend.

export const SORT_OPTIONS = Object.freeze([
  { value: "-applied_date", label: "Newest applications" },
  { value: "applied_date", label: "Oldest applications" },
  { value: "name", label: "Name A–Z" },
  { value: "-name", label: "Name Z–A" },
  { value: "-years_experience", label: "Most experience" },
  { value: "years_experience", label: "Least experience" },
]);

export const SHORTLIST_OPTIONS = Object.freeze([
  { value: "", label: "Everyone" },
  { value: "true", label: "Shortlisted" },
  { value: "false", label: "Not shortlisted" },
]);

export const PAGE_SIZES = Object.freeze([20, 50, 100]);

export const FIT_LABELS = Object.freeze({
  strong: "Strong fit",
  moderate: "Moderate fit",
  weak: "Weak fit",
  insufficient_evidence: "Insufficient evidence",
});

export const ROLE_LABELS = Object.freeze({
  admin: "Admin",
  hiring_manager: "Hiring manager",
  recruiter: "Recruiter",
});

export const VIEWS = Object.freeze(["browse", "rank", "activity", "users"]);

const ACTION_TEXT = Object.freeze({
  tag_added: "added tag",
  tag_removed: "removed tag",
  shortlist_added: "shortlisted",
  shortlist_removed: "removed from shortlist",
  summary_generated: "generated an AI summary for",
  login: "signed in",
  logout: "signed out",
  login_failed: "failed sign-in",
  login_throttled: "sign-in blocked (too many attempts)",
  login_blocked_inactive: "sign-in refused (deactivated)",
  user_created: "created user",
  user_role_changed: "changed role of",
  user_activated: "activated",
  user_deactivated: "deactivated",
  password_reset: "reset the password of",
  password_changed: "changed their password",
  password_change_failed: "failed password change",
});

/** "added tag “follow-up” · Siddharth Mehta" style text for one audit entry. */
export function describeAction(entry) {
  const verb = ACTION_TEXT[entry.action] || entry.action.replace(/_/g, " ");
  const details = entry.details || {};
  const parts = [verb];
  if (details.tag) parts.push(`“${details.tag}”`);
  if (entry.target_type === "candidate") parts.push(entry.candidate_name || entry.target_id);
  if (entry.target_type === "user" && details.email) parts.push(details.email);
  if (entry.action === "user_role_changed" && details.to) {
    parts.push(`(${ROLE_LABELS[details.from] || details.from} → ${ROLE_LABELS[details.to] || details.to})`);
  }
  if (entry.action === "user_created" && details.role) parts.push(`as ${ROLE_LABELS[details.role] || details.role}`);
  if (entry.action === "summary_generated" && details.fit) parts.push(`(${fitLabel(details.fit)})`);
  return parts.join(" ");
}

/** Where to go after signing in: only same-site paths, never another site or the sign-in page itself. */
export function safeNextPath(next) {
  if (typeof next !== "string" || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) return "/";
  if (/[\u0000-\u001f]/.test(next) || next.startsWith("/login")) return "/";
  return next;
}

export const BROWSE_DEFAULTS = Object.freeze({
  q: "", role: "", source: "", tag: "", shortlisted: "", sort: "-applied_date", limit: 20, offset: 0,
});

export const RANK_DEFAULTS = Object.freeze({ skills: [], role: "", source: "", limit: 20, offset: 0 });

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "?a=1&b=x&b=y" from an object; skips empty values, repeats arrays. "" when nothing is set. */
export function buildQuery(params = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    const values = Array.isArray(value) ? value : [value];
    for (const item of values) {
      if (item === undefined || item === null || item === "") continue;
      search.append(key, String(item));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function formatYears(years) {
  if (years === null || years === undefined) return "Not stated";
  return `${years} ${years === 1 ? "yr" : "yrs"}`;
}

/** "2026-09-10" -> "10 Sep 2026". Parsed by hand so the browser's time zone can't shift the day. */
export function formatDate(iso) {
  if (!iso) return "Not stated";
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  const month = match && MONTHS[Number(match[2]) - 1];
  return month ? `${Number(match[3])} ${month} ${match[1]}` : String(iso);
}

/** "2026-10-26T14:01:05+00:00" -> "26 Oct 2026, 14:01 UTC" (the API sends UTC). */
export function formatDateTime(iso) {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/.exec(iso || "");
  return match ? `${formatDate(match[1])}, ${match[2]}:${match[3]} UTC` : String(iso || "");
}

/** Labels and button states for a page of `limit` results starting at `offset`. */
export function pageInfo(total, limit, offset) {
  if (!total) return { label: "No results", hasPrev: offset > 0, hasNext: false, page: 1, pages: 1 };
  const pages = Math.max(1, Math.ceil(total / limit));
  const page = Math.floor(offset / limit) + 1;
  const label = offset >= total
    ? `Past the last result (${total} total)`
    : `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`;
  return { label, hasPrev: offset > 0, hasNext: offset + limit < total, page, pages };
}

/** A readable message from an API error body (FastAPI's `detail` string or validation list). */
export function formatApiError(status, body) {
  const detail = body && body.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((item) => {
      const field = (item.loc || []).filter((part) => !["body", "query", "path"].includes(part)).join(".");
      const message = String(item.msg || "Invalid value").replace(/^Value error, /, "");
      return field ? `${field}: ${message}` : message;
    }).join("; ");
  }
  if (status === 0) return "Could not reach the server. Is it running?";
  if (status === 404) return "Not found.";
  if (status >= 500) return `The server had a problem (HTTP ${status}). Try again.`;
  return `Request failed (HTTP ${status}).`;
}

/** Split "Python, FastAPI" typed into the skill box into separate entries. */
export function splitSkillInput(text) {
  return String(text || "").split(",").map((part) => part.trim().replace(/\s+/g, " ")).filter(Boolean);
}

/** Append items not already in the list, ignoring case. Returns a new array. */
export function addUnique(list, items) {
  const result = [...list];
  const seen = new Set(list.map((item) => item.toLowerCase()));
  for (const item of items) {
    if (!seen.has(item.toLowerCase())) {
      seen.add(item.toLowerCase());
      result.push(item);
    }
  }
  return result;
}

export function fitLabel(fit) {
  return FIT_LABELS[fit] || fit;
}

export function coveragePercent(coverage) {
  return Math.round(Math.min(1, Math.max(0, Number(coverage) || 0)) * 100);
}

/** First `max` skills to show, plus the rest for a "+N" chip. */
export function visibleSkills(skills, max = 4) {
  const shown = skills.length > max ? skills.slice(0, max - 1) : skills.slice();
  return { shown, hidden: skills.slice(shown.length) };
}

function intFrom(value, fallback, allowed) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 0) return fallback;
  return allowed && !allowed.includes(number) ? fallback : number;
}

/** View and filter state from the page URL, ignoring anything invalid. */
export function readUrlState(search) {
  const params = new URLSearchParams(search);
  const view = VIEWS.includes(params.get("view")) ? params.get("view") : "browse";
  const browse = { ...BROWSE_DEFAULTS };
  const rank = { ...RANK_DEFAULTS, skills: [] };
  if (view === "activity" || view === "users") return { view, browse, rank };
  if (view === "browse") {
    for (const key of ["q", "role", "source", "tag"]) browse[key] = params.get(key) || "";
    const sort = params.get("sort");
    if (SORT_OPTIONS.some((option) => option.value === sort)) browse.sort = sort;
    const shortlisted = params.get("shortlisted");
    if (shortlisted === "true" || shortlisted === "false") browse.shortlisted = shortlisted;
    browse.limit = intFrom(params.get("limit"), BROWSE_DEFAULTS.limit, PAGE_SIZES);
    browse.offset = intFrom(params.get("offset"), 0);
  } else {
    rank.skills = addUnique([], params.getAll("skills").flatMap(splitSkillInput));
    rank.role = params.get("role") || "";
    rank.source = params.get("source") || "";
    rank.offset = intFrom(params.get("offset"), 0);
  }
  return { view, browse, rank };
}

/** The URL query for the current view, keeping only values that differ from the defaults. */
export function writeUrlState(view, browse, rank) {
  if (view === "activity" || view === "users") return buildQuery({ view });
  if (view === "rank") {
    return buildQuery({ view: "rank", skills: rank.skills, role: rank.role, source: rank.source, offset: rank.offset || "" });
  }
  const changed = {};
  for (const [key, value] of Object.entries(browse)) {
    if (value !== BROWSE_DEFAULTS[key]) changed[key] = value;
  }
  return buildQuery(changed);
}
