// Recruiter dashboard: renders API data and sends recruiter actions back.
// All text goes into the page through textContent (via el()), never innerHTML.
// Controls are shown or hidden by the signed-in user's permissions for convenience
// only; the server checks every permission again on every request.
import { api, ApiError, onUnauthorized, setCsrfToken } from "./api.js";
import {
  BROWSE_DEFAULTS, PAGE_SIZES, ROLE_LABELS, SHORTLIST_OPTIONS, SORT_OPTIONS,
  addUnique, coveragePercent, describeAction, fitLabel, formatDate, formatDateTime, formatYears, pageInfo,
  readUrlState, splitSkillInput, visibleSkills, writeUrlState,
} from "./utils.js";

const initial = readUrlState(location.search);
const state = {
  view: initial.view,
  browse: initial.browse,
  rank: initial.rank,
  activityOffset: 0,
  facets: null,
  health: null,
  user: null,
  permissions: new Set(),
  selectedId: null,
  detail: null,
  summaryContext: [], // required skills carried over from the ranking that opened the candidate
};
const VIEW_PERMISSION = { browse: "candidates:read", rank: "ranking:read", activity: "activity:read", users: "users:manage" };
const ACTIVITY_PAGE = 25;
let signingOut = false;

const can = (permission) => state.permissions.has(permission);
const inflight = { browse: null, rank: null, detail: null };
let summaryToken = 0;
let searchTimer = null;

const $ = (selector) => document.querySelector(selector);

/** createElement with props: class, text, on<event>, dataset, booleans; children may be nested arrays. */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function chip(text, kind = "", title) {
  return el("span", { class: `chip ${kind}`.trim(), title }, text);
}

function messageOf(error) {
  return error instanceof ApiError ? error.message : "Something went wrong. Try again.";
}

function toast(message, kind = "info") {
  const node = el("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" }, message);
  $("#toasts").append(node);
  setTimeout(() => node.remove(), kind === "error" ? 6000 : 3500);
}

function spinnerBlock(message) {
  return el("div", { class: "state state-loading", role: "status" }, el("span", { class: "spinner", "aria-hidden": "true" }), message);
}

function errorBlock(message, retry) {
  return el("div", { class: "state state-error", role: "alert" },
    el("p", {}, message),
    retry ? el("button", { type: "button", class: "btn", onclick: retry }, "Try again") : null);
}

function emptyBlock(title, text, action) {
  return el("div", { class: "state state-empty" }, el("p", { class: "state-title" }, title), text ? el("p", {}, text) : null, action);
}

/** Dim the current results while a reload runs, or show a spinner if there is nothing yet. */
function showLoading(container, message) {
  container.setAttribute("aria-busy", "true");
  if (container.querySelector("table")) container.classList.add("is-loading");
  else container.replaceChildren(spinnerBlock(message));
}

function doneLoading(container) {
  container.removeAttribute("aria-busy");
  container.classList.remove("is-loading");
}

function updateUrl() {
  history.replaceState(null, "", location.pathname + writeUrlState(state.view, state.browse, state.rank));
}

// --- Status, facets and filter controls -----------------------------------

async function loadHealth() {
  const status = $("#status");
  try {
    const { data } = await api.health();
    state.health = data;
    status.replaceChildren(
      el("span", { class: "status-item" }, `${data.candidates} candidates`),
      el("span", { class: `status-item ${data.ai_configured ? "ok" : "warn"}` },
        el("span", { class: "dot", "aria-hidden": "true" }), data.ai_configured ? "AI ready" : "AI not configured"));
    if (data.candidates === 0) {
      showBanner("No candidates in the database yet. Run python init_db.py, then reload this page.");
    }
  } catch (error) {
    state.health = null;
    status.replaceChildren(el("span", { class: "status-item warn" }, el("span", { class: "dot", "aria-hidden": "true" }), "Server problem"));
    showBanner(error instanceof ApiError && error.body && error.body.db === "error"
      ? "The database can't be read. Check data/candidates.db and restart the server."
      : messageOf(error));
  }
}

function showBanner(message) {
  const banner = $("#banner");
  banner.textContent = message;
  banner.hidden = false;
}

async function loadFacets() {
  try {
    const { data } = await api.facets();
    state.facets = data;
    fillSelect($("#f-role"), "All roles", data.roles, state.browse.role);
    fillSelect($("#f-source"), "All sources", data.sources, state.browse.source);
    fillSelect($("#f-tag"), "All tags", data.tags, state.browse.tag);
    fillSelect($("#r-role"), "All roles", data.roles, state.rank.role);
    fillSelect($("#r-source"), "All sources", data.sources, state.rank.source);
    $("#skill-options").replaceChildren(...data.skills.map((s) => el("option", { value: s.value })));
    $("#tag-options").replaceChildren(...data.tags.map((t) => el("option", { value: t.value })));
  } catch (error) {
    toast(`Filters couldn't load: ${messageOf(error)}`, "error");
  }
}

function fillSelect(select, allLabel, facetValues, current) {
  const options = [el("option", { value: "" }, allLabel)];
  for (const { value, count } of facetValues) options.push(el("option", { value }, `${value} (${count})`));
  if (current && !facetValues.some((f) => f.value === current)) options.push(el("option", { value: current }, `${current} (0)`));
  select.replaceChildren(...options);
  select.value = current || "";
}

function fillStaticSelect(select, options, current) {
  select.replaceChildren(...options.map((o) => el("option", { value: o.value }, o.label)));
  select.value = current;
}

// --- Views ------------------------------------------------------------------

function allowedViews() {
  return Object.keys(VIEW_PERMISSION).filter((view) => can(VIEW_PERMISSION[view]));
}

function showView(view) {
  if (!allowedViews().includes(view)) view = "browse";
  state.view = view;
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.view === view;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  for (const name of Object.keys(VIEW_PERMISSION)) $(`#view-${name}`).hidden = name !== view;
  updateUrl();
  if (view === "browse") loadBrowse();
  else if (view === "rank") loadRank();
  else if (view === "activity") loadActivity();
  else loadUsers();
}

function reloadCurrentView() {
  if (state.view === "browse") loadBrowse({ quiet: true });
  else if (state.view === "rank") loadRank({ quiet: true });
  else if (state.view === "activity") loadActivity();
}

// --- Browse -----------------------------------------------------------------

async function loadBrowse({ quiet = false } = {}) {
  const results = $("#browse-results");
  inflight.browse?.abort();
  const controller = new AbortController();
  inflight.browse = controller;
  if (!quiet) showLoading(results, "Loading candidates…");
  updateUrl();
  try {
    const { data } = await api.listCandidates(state.browse, controller.signal);
    renderBrowse(data);
  } catch (error) {
    if (error.name === "AbortError") return;
    $("#browse-count").textContent = "";
    $("#browse-pager").hidden = true;
    results.replaceChildren(errorBlock(`Candidates couldn't load. ${messageOf(error)}`, () => loadBrowse()));
  } finally {
    if (inflight.browse === controller) doneLoading(results);
  }
}

function filtersActive() {
  const { q, role, source, tag, shortlisted } = state.browse;
  return Boolean(q || role || source || tag || shortlisted);
}

function renderBrowse(page) {
  const results = $("#browse-results");
  $("#browse-count").textContent = page.total === 1 ? "1 candidate" : `${page.total} candidates`;
  if (!page.items.length) {
    results.replaceChildren(page.total
      ? emptyBlock("No candidates on this page.", null, el("button", { type: "button", class: "btn", onclick: () => setBrowse({ offset: 0 }) }, "Back to the first page"))
      : filtersActive()
        ? emptyBlock("No candidates match these filters.", "Try a different search, or clear the filters.", el("button", { type: "button", class: "btn", onclick: clearFilters }, "Clear filters"))
        : emptyBlock("No candidates yet.", "Import them with python init_db.py, then reload."));
    renderPager($("#browse-pager"), page, (offset) => setBrowse({ offset }));
    return;
  }
  const rows = page.items.map((c) => el("tr", {
    dataset: { id: c.id },
    class: c.id === state.selectedId ? "selected" : null,
    onclick: (event) => { if (!event.target.closest("button")) selectCandidate(c.id); },
  },
  nameCell(c, c.id),
  el("td", { class: "col-role" }, c.target_role),
  el("td", { class: "col-skills" }, skillChips(c.skills)),
  el("td", { class: `col-exp ${c.years_experience == null ? "muted" : ""}` }, formatYears(c.years_experience)),
  el("td", { class: "col-source" }, c.source),
  el("td", { class: `col-applied ${c.applied_date ? "" : "muted"}` }, formatDate(c.applied_date))));

  results.replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "table" },
    el("caption", { class: "visually-hidden" }, "Candidates. Select a name to see details."),
    el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, "Candidate"), el("th", { scope: "col", class: "col-role" }, "Role"),
      el("th", { scope: "col", class: "col-skills" }, "Skills"), el("th", { scope: "col", class: "col-exp" }, "Experience"),
      el("th", { scope: "col", class: "col-source" }, "Source"), el("th", { scope: "col", class: "col-applied" }, "Applied"))),
    el("tbody", {}, rows))));
  renderPager($("#browse-pager"), page, (offset) => setBrowse({ offset }), true);
}

function nameCell(c, subtitle, onSelect = () => selectCandidate(c.id)) {
  return el("td", { class: "col-name" },
    el("button", { type: "button", class: "name-link", onclick: onSelect }, c.name),
    c.shortlisted ? el("span", { class: "badge badge-star", title: "Shortlisted" }, "★ Shortlisted") : null,
    el("div", { class: "sub" }, subtitle),
    c.tags.length ? el("div", { class: "tag-row" }, c.tags.map((t) => chip(t, "chip-tag chip-small"))) : null);
}

function skillChips(skills) {
  if (!skills.length) return el("span", { class: "muted" }, "None listed");
  const { shown, hidden } = visibleSkills(skills, 4);
  return el("div", { class: "chip-row" }, shown.map((s) => chip(s)),
    hidden.length ? chip(`+${hidden.length}`, "chip-more", hidden.join(", ")) : null);
}

function setBrowse(changes, { resetOffset = false } = {}) {
  Object.assign(state.browse, changes, resetOffset ? { offset: 0 } : {});
  loadBrowse();
}

function clearFilters() {
  Object.assign(state.browse, { ...BROWSE_DEFAULTS, sort: state.browse.sort, limit: state.browse.limit });
  syncBrowseControls();
  loadBrowse();
}

function syncBrowseControls() {
  $("#f-q").value = state.browse.q;
  for (const key of ["role", "source", "tag", "shortlisted", "sort"]) $(`#f-${key}`).value = state.browse[key];
}

function renderPager(nav, { total, limit, offset }, onPage, withPageSize = false) {
  const info = pageInfo(total, limit, offset);
  nav.hidden = total === 0 && offset === 0;
  const parts = [
    el("button", { type: "button", class: "btn", disabled: !info.hasPrev, onclick: () => onPage(Math.max(0, offset - limit)) }, "← Previous"),
    el("span", { class: "pager-label" }, info.label, total ? el("span", { class: "muted" }, ` · page ${info.page} of ${info.pages}`) : null),
    el("button", { type: "button", class: "btn", disabled: !info.hasNext, onclick: () => onPage(offset + limit) }, "Next →"),
  ];
  if (withPageSize) {
    const select = el("select", { onchange: (e) => setBrowse({ limit: Number(e.target.value) }, { resetOffset: true }) },
      PAGE_SIZES.map((n) => el("option", { value: n }, n)));
    select.value = String(limit);
    parts.push(el("label", { class: "page-size" }, "Per page ", select));
  }
  // replaceChildren() would print a null argument as the text "null", so only real nodes are passed.
  nav.replaceChildren(...parts);
}

// --- Rank -------------------------------------------------------------------

function renderRankChips() {
  $("#rank-chip-list").replaceChildren(...state.rank.skills.map((skill) => el("li", { class: "chip chip-input-item" }, skill,
    el("button", { type: "button", class: "chip-x", "aria-label": `Remove ${skill}`, onclick: () => removeRankSkill(skill) }, "×"))));
}

function commitRankInput() {
  const input = $("#rank-skill");
  const added = splitSkillInput(input.value);
  input.value = "";
  if (!added.length) return false;
  const before = state.rank.skills.length;
  state.rank.skills = addUnique(state.rank.skills, added).slice(0, 20);
  if (state.rank.skills.length === 20 && before + added.length > 20) toast("Up to 20 skills can be ranked at once.", "error");
  renderRankChips();
  return state.rank.skills.length !== before;
}

function removeRankSkill(skill) {
  state.rank.skills = state.rank.skills.filter((s) => s !== skill);
  state.rank.offset = 0;
  renderRankChips();
  loadRank();
}

async function loadRank({ quiet = false } = {}) {
  const results = $("#rank-results");
  inflight.rank?.abort();
  updateUrl();
  if (!state.rank.skills.length) {
    $("#rank-notice").replaceChildren();
    $("#rank-count").textContent = "";
    $("#rank-pager").hidden = true;
    results.replaceChildren(emptyBlock("Rank candidates by the skills you need.",
      "Add one or more required skills above. Candidates are scored by how many they have, then by experience."));
    return;
  }
  const controller = new AbortController();
  inflight.rank = controller;
  if (!quiet) showLoading(results, "Ranking candidates…");
  const { skills, role, source, limit, offset } = state.rank;
  try {
    const { data } = await api.rank({ skills, role, source, limit, offset }, controller.signal);
    renderRank(data);
  } catch (error) {
    if (error.name === "AbortError") return;
    $("#rank-count").textContent = "";
    $("#rank-pager").hidden = true;
    results.replaceChildren(errorBlock(`Ranking failed. ${messageOf(error)}`, () => loadRank()));
  } finally {
    if (inflight.rank === controller) doneLoading(results);
  }
}

/** Width is set through the style property: the Content Security Policy blocks inline style attributes. */
function meter(percent) {
  const fill = el("span");
  fill.style.width = `${percent}%`;
  return el("div", { class: "meter", role: "img", "aria-label": `${percent}% of required skills` }, fill);
}

function renderRank(result) {
  const results = $("#rank-results");
  const notice = $("#rank-notice");
  notice.replaceChildren(result.unknown_skills.length
    ? el("div", { class: "notice", role: "status" }, "No candidate lists ",
      result.unknown_skills.map((s, i) => [i ? ", " : "", el("strong", {}, s)]),
      ". ", result.query_skills.length ? "Ranking uses the other skills." : "Check the spelling, or pick a skill from the suggestions.")
    : "");
  const total = result.query_skills.length;
  $("#rank-count").textContent = total
    ? `${result.total} ${result.total === 1 ? "candidate has" : "candidates have"} at least one of ${total} required ${total === 1 ? "skill" : "skills"}`
    : "";
  if (!result.items.length) {
    results.replaceChildren(total
      ? emptyBlock("No candidates have these skills.", state.rank.role || state.rank.source ? "Try removing the role or source filter." : "Try different or fewer skills.")
      : emptyBlock("Nothing to rank.", "None of the requested skills appear in the candidate data."));
    renderPager($("#rank-pager"), result, (offset) => { state.rank.offset = offset; loadRank(); });
    return;
  }
  const rows = result.items.map((item, index) => {
    const c = item.candidate;
    return el("tr", {
      dataset: { id: c.id },
      class: c.id === state.selectedId ? "selected" : null,
      onclick: (event) => { if (!event.target.closest("button")) selectCandidate(c.id, result.query_skills); },
    },
    el("td", { class: "col-rank" }, `#${result.offset + index + 1}`),
    nameCell(c, c.target_role, () => selectCandidate(c.id, result.query_skills)),
    el("td", { class: "col-match" },
      el("div", { class: "score" }, el("strong", {}, `${item.score}/${total}`), el("span", { class: "muted" }, ` ${coveragePercent(item.coverage)}%`)),
      meter(coveragePercent(item.coverage))),
    el("td", { class: "col-skills" }, el("div", { class: "chip-row" }, item.matched_skills.map((s) => chip(`✓ ${s}`, "chip-match")))),
    el("td", { class: "col-missing" }, item.missing_skills.length
      ? el("div", { class: "chip-row" }, item.missing_skills.map((s) => chip(s, "chip-miss")))
      : el("span", { class: "muted" }, "None")),
    el("td", { class: `col-exp ${c.years_experience == null ? "muted" : ""}` }, formatYears(c.years_experience)));
  });
  results.replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "table" },
    el("caption", { class: "visually-hidden" }, "Ranked candidates. Select a name to see details and an AI summary against these skills."),
    el("thead", {}, el("tr", {},
      el("th", { scope: "col", class: "col-rank" }, "Rank"), el("th", { scope: "col" }, "Candidate"),
      el("th", { scope: "col", class: "col-match" }, "Match"), el("th", { scope: "col", class: "col-skills" }, "Has"),
      el("th", { scope: "col", class: "col-missing" }, "Missing"), el("th", { scope: "col", class: "col-exp" }, "Experience"))),
    el("tbody", {}, rows))));
  renderPager($("#rank-pager"), result, (offset) => { state.rank.offset = offset; loadRank(); });
}

// --- Detail panel -----------------------------------------------------------

function markSelectedRows() {
  for (const row of document.querySelectorAll("tr[data-id]")) row.classList.toggle("selected", row.dataset.id === state.selectedId);
}

function selectCandidate(id, requirements = []) {
  state.selectedId = id;
  state.summaryContext = [...requirements];
  summaryToken += 1;
  markSelectedRows();
  $("#detail").hidden = false;
  $("#backdrop").hidden = false;
  document.body.classList.add("detail-open");
  loadDetail();
}

function closeDetail() {
  inflight.detail?.abort();
  summaryToken += 1;
  state.selectedId = null;
  state.detail = null;
  $("#detail").hidden = true;
  $("#backdrop").hidden = true;
  document.body.classList.remove("detail-open");
  markSelectedRows();
}

async function loadDetail() {
  const body = $("#detail-body");
  const id = state.selectedId;
  inflight.detail?.abort();
  const controller = new AbortController();
  inflight.detail = controller;
  body.replaceChildren(detailHeader("Loading…", ""), spinnerBlock("Loading candidate…"));
  try {
    const { data } = await api.getCandidate(id, controller.signal);
    if (state.selectedId !== id) return;
    state.detail = data;
    renderDetail(data);
  } catch (error) {
    if (error.name === "AbortError") return;
    body.replaceChildren(detailHeader("Candidate", ""), errorBlock(
      error instanceof ApiError && error.status === 404 ? "This candidate no longer exists." : `Details couldn't load. ${messageOf(error)}`,
      () => loadDetail()));
  }
}

function detailHeader(title, subtitle) {
  return el("div", { class: "detail-head" },
    el("div", {}, el("h2", { id: "detail-title", tabindex: "-1" }, title), subtitle ? el("p", { class: "sub" }, subtitle) : null),
    el("button", { type: "button", class: "icon-btn", "aria-label": "Close details", onclick: closeDetail }, "×"));
}

function section(title, ...content) {
  return el("section", { class: "detail-section" }, el("h3", {}, title), content);
}

function renderDetail(c) {
  const required = new Set(state.summaryContext.map((s) => s.toLowerCase()));
  $("#detail-body").replaceChildren(
    detailHeader(c.name, `${c.target_role} · ${c.id}`),
    el("div", { id: "detail-shortlist", class: "detail-actions" }),
    el("dl", { class: "meta" },
      el("div", {}, el("dt", {}, "Experience"), el("dd", {}, formatYears(c.years_experience))),
      el("div", {}, el("dt", {}, "Source"), el("dd", {}, c.source)),
      el("div", {}, el("dt", {}, "Applied"), el("dd", {}, formatDate(c.applied_date)))),
    section("Tags", el("div", { id: "detail-tags" })),
    section("Skills", c.skills.length
      ? el("div", { class: "chip-row" }, c.skills.map((s) => chip(s, required.has(s.toLowerCase()) ? "chip-match" : "")))
      : el("p", { class: "muted" }, "No skills listed.")),
    section("Recruiter notes", el("p", { class: "notes" }, c.notes || "No notes recorded.")),
    summarySection(c) || "");
  renderShortlist(c);
  renderTags(c);
  $("#detail-title").focus({ preventScroll: true });
}

function renderShortlist(c) {
  if (!can("shortlist:write")) {
    $("#detail-shortlist").replaceChildren(c.shortlisted
      ? el("span", { class: "badge badge-star badge-large" }, "★ Shortlisted")
      : el("span", { class: "muted small" }, "Not shortlisted"));
    return;
  }
  const button = el("button", {
    type: "button",
    class: `btn btn-shortlist ${c.shortlisted ? "is-on" : ""}`,
    "aria-pressed": String(c.shortlisted),
    onclick: () => toggleShortlist(button),
  }, c.shortlisted ? "★ Shortlisted" : "☆ Add to shortlist");
  $("#detail-shortlist").replaceChildren(button);
}

async function toggleShortlist(button) {
  const c = state.detail;
  button.disabled = true;
  try {
    const { data } = await api.setShortlisted(c.id, !c.shortlisted);
    if (state.detail !== c) return;
    c.shortlisted = data.shortlisted;
    renderShortlist(c);
    toast(data.shortlisted ? `${c.name} added to the shortlist.` : `${c.name} removed from the shortlist.`, "success");
    afterRecruiterChange();
  } catch (error) {
    button.disabled = false;
    toast(`Shortlist not updated. ${messageOf(error)}`, "error");
  }
}

function renderTags(c) {
  if (!can("tags:write")) {
    $("#detail-tags").replaceChildren(c.tags.length
      ? el("div", { class: "chip-row" }, c.tags.map((tag) => chip(tag, "chip-tag")))
      : el("p", { class: "muted" }, "No tags."));
    return;
  }
  const error = el("p", { class: "field-error", role: "alert" });
  const input = el("input", { type: "text", maxlength: "40", list: "tag-options", placeholder: "e.g. strong backend", "aria-label": "New tag" });
  const form = el("form", {
    class: "inline-form",
    onsubmit: (event) => { event.preventDefault(); addTag(c, input, form, error); },
  }, input, el("button", { type: "submit", class: "btn" }, "Add tag"));
  $("#detail-tags").replaceChildren(
    c.tags.length
      ? el("ul", { class: "chip-list", "aria-label": "Tags" }, c.tags.map((tag) => el("li", { class: "chip chip-tag" }, tag,
        el("button", { type: "button", class: "chip-x", "aria-label": `Remove tag ${tag}`, onclick: (e) => removeTag(c, tag, e.currentTarget) }, "×"))))
      : el("p", { class: "muted" }, "No tags yet."),
    form, error);
}

async function addTag(c, input, form, errorNode) {
  const tag = input.value.trim();
  if (!tag) {
    errorNode.textContent = "Type a tag first.";
    return;
  }
  for (const control of form.elements) control.disabled = true;
  try {
    const { status, data } = await api.addTag(c.id, tag);
    if (state.detail !== c) return;
    c.tags = data.tags;
    renderTags(c);
    toast(status === 201 ? "Tag added." : "That tag was already there.", status === 201 ? "success" : "info");
    afterRecruiterChange();
  } catch (error) {
    for (const control of form.elements) control.disabled = false;
    errorNode.textContent = messageOf(error);
    input.focus();
  }
}

async function removeTag(c, tag, button) {
  button.disabled = true;
  try {
    const { data } = await api.removeTag(c.id, tag);
    if (state.detail !== c) return;
    c.tags = data.tags;
    renderTags(c);
    toast(`Tag “${tag}” removed.`, "success");
    afterRecruiterChange();
  } catch (error) {
    button.disabled = false;
    toast(`Tag not removed. ${messageOf(error)}`, "error");
  }
}

function afterRecruiterChange() {
  loadFacets();
  reloadCurrentView();
}

// --- AI summary -------------------------------------------------------------

function summarySection(c) {
  const context = state.summaryContext;
  const generate = can("summaries:generate");
  const aiOff = generate && state.health && !state.health.ai_configured;
  if (!generate && !can("summaries:read")) return null;
  return el("section", { class: "detail-section summary-section", "aria-labelledby": "summary-title" },
    el("div", { class: "summary-head" },
      el("h3", { id: "summary-title" }, "AI fit summary"),
      generate
        ? el("button", { type: "button", id: "summary-run", class: "btn btn-primary", onclick: () => runSummary(false) }, "Generate summary")
        : el("button", { type: "button", id: "summary-run", class: "btn", onclick: loadCachedSummary }, "Load summary")),
    generate ? null : el("p", { class: "muted small" }, "Your role can view summaries a recruiter has generated, but not generate new ones."),
    el("p", { class: "muted small" }, context.length
      ? ["Judged against the target role (", c.target_role, ") and the ranking's required skills: ", context.join(", "), ". "]
      : ["Judged against the target role (", c.target_role, "). Open a candidate from “Rank by skills” to include required skills. "],
    "Every strength and concern quotes the candidate's record."),
    aiOff ? el("p", { class: "notice small" }, "AI isn't configured on the server (GROQ_API_KEY). Summaries generated earlier can still load.") : null,
    el("div", { id: "summary-result", "aria-live": "polite" }));
}

async function runSummary(refresh) {
  const id = state.selectedId;
  const token = ++summaryToken;
  const result = $("#summary-result");
  const runButton = $("#summary-run");
  runButton.disabled = true;
  result.replaceChildren(spinnerBlock(refresh ? "Generating a fresh summary… usually 2–10 seconds" : "Generating summary… usually 2–10 seconds (instant if cached)"));
  try {
    const { data } = await api.summary(id, { requirements: state.summaryContext, refresh });
    if (token !== summaryToken || state.selectedId !== id) return;
    result.replaceChildren(renderSummary(data));
    result.scrollIntoView({ behavior: "smooth", block: "nearest" });
    runButton.textContent = "Generate again";
    runButton.onclick = () => runSummary(true);
  } catch (error) {
    if (token !== summaryToken || state.selectedId !== id) return;
    result.replaceChildren(errorBlock(summaryErrorMessage(error), () => runSummary(refresh)));
  } finally {
    if (token === summaryToken) runButton.disabled = false;
  }
}

async function loadCachedSummary() {
  const id = state.selectedId;
  const token = ++summaryToken;
  const result = $("#summary-result");
  const button = $("#summary-run");
  button.disabled = true;
  result.replaceChildren(spinnerBlock("Loading summary…"));
  try {
    const { data } = await api.cachedSummary(id, state.summaryContext);
    if (token !== summaryToken || state.selectedId !== id) return;
    result.replaceChildren(renderSummary(data));
    result.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    if (token !== summaryToken || state.selectedId !== id) return;
    result.replaceChildren(error instanceof ApiError && error.status === 404 && state.detail
      ? emptyBlock("No summary yet.", state.summaryContext.length
        ? "None has been generated for this candidate with these required skills. A recruiter can generate one."
        : "A recruiter can generate one from this candidate's page.")
      : errorBlock(summaryErrorMessage(error), loadCachedSummary));
  } finally {
    if (token === summaryToken) button.disabled = false;
  }
}

function summaryErrorMessage(error) {
  if (!(error instanceof ApiError)) return messageOf(error);
  if (error.status === 404) return "This candidate no longer exists.";
  return `No summary this time: ${error.message}`;
}

function claimList(title, claims, emptyText, withSource = false) {
  return el("div", { class: "summary-block" },
    el("h4", {}, title, el("span", { class: "count" }, String(claims.length))),
    claims.length
      ? el("ul", { class: "claims" }, claims.map((claim) => el("li", {},
        el("p", { class: "claim" }, claim.claim,
          withSource ? el("span", { class: `badge ${claim.source === "data_check" ? "badge-check" : "badge-ai"}` },
            claim.source === "data_check" ? "Data check" : "AI") : null),
        el("blockquote", { class: "evidence" }, `“${claim.evidence}”`))))
      : el("p", { class: "muted small" }, emptyText));
}

function gapRow(label, skills, kind, note) {
  if (!skills.length) return null;
  return el("div", { class: "gap-row" }, el("span", { class: "gap-label" }, label, note ? el("span", { class: "muted" }, ` ${note}`) : null),
    el("div", { class: "chip-row" }, skills.map((s) => chip(s, kind))));
}

function renderSummary(s) {
  const gaps = s.skill_gaps;
  const anyGaps = gaps.required_missing.length || gaps.required_in_notes_only.length || gaps.role_gaps.length;
  const { claims_returned: returned, claims_dropped: dropped } = s.grounding;
  return el("div", { class: "summary" },
    el("div", { class: "summary-top" },
      el("span", { class: `fit fit-${s.fit}` }, fitLabel(s.fit)),
      el("span", { class: "muted small" }, s.cached ? "From cache · " : "", `Generated ${formatDateTime(s.generated_at)} · ${s.model}`)),
    el("p", { class: "summary-text" }, s.summary),
    claimList("Strengths", s.strengths, "No evidence-backed strengths."),
    claimList("Concerns", s.concerns, "No evidence-backed concerns."),
    claimList("Contradictions", s.contradictions, "No contradictions found between the notes and the structured data.", true),
    el("div", { class: "summary-block" }, el("h4", {}, "Skill gaps"),
      anyGaps
        ? [gapRow("Required but not listed", gaps.required_missing, "chip-miss"),
          gapRow("Mentioned in notes only", gaps.required_in_notes_only, "chip-warn", "(not in the skills list)"),
          gapRow("Usual for the role", gaps.role_gaps, "", "(AI judgement)")]
        : el("p", { class: "muted small" }, "No skill gaps found.")),
    s.missing_information.length
      ? el("div", { class: "summary-block" }, el("h4", {}, "Still to find out"), el("ul", { class: "plain-list" }, s.missing_information.map((m) => el("li", {}, m))))
      : null,
    dropped
      ? el("details", { class: "grounding warn" },
        el("summary", {}, `${dropped} of ${returned} AI claims removed: their evidence wasn't found in the record`),
        el("ul", { class: "plain-list" }, s.grounding.dropped.map((d) => el("li", {}, `${d.claim} (quoted “${d.evidence}”)`))))
      : el("p", { class: "grounding ok small" }, returned
        ? `All ${returned} AI claims quote the candidate's record.`
        : "The AI made no evidence-backed claims for this candidate."));
}

// --- Activity (hiring managers and admins) ----------------------------------

async function loadActivity() {
  const results = $("#activity-results");
  const fullLog = can("audit:read");
  $("#activity-title").textContent = fullLog ? "Audit log" : "Recruitment activity";
  $("#activity-intro").textContent = fullLog
    ? "Every recorded action, newest first: sign-ins, account changes, tags, shortlist changes and AI summaries."
    : "Tags, shortlist changes and AI summaries by recruiters, newest first.";
  showLoading(results, "Loading activity…");
  const params = { limit: ACTIVITY_PAGE, offset: state.activityOffset };
  try {
    const { data } = await (fullLog ? api.auditLogs(params) : api.activity(params));
    renderActivity(data, fullLog);
  } catch (error) {
    $("#activity-pager").hidden = true;
    results.replaceChildren(errorBlock(`Activity couldn't load. ${messageOf(error)}`, loadActivity));
  } finally {
    doneLoading(results);
  }
}

function renderActivity(page, fullLog) {
  const results = $("#activity-results");
  const pageTo = (offset) => { state.activityOffset = offset; loadActivity(); };
  if (!page.items.length) {
    results.replaceChildren(emptyBlock("No activity yet.", "Actions appear here as recruiters tag, shortlist and summarize candidates."));
    renderPager($("#activity-pager"), page, pageTo);
    return;
  }
  const rows = page.items.map((entry) => el("tr", {},
    el("td", { class: "col-applied" }, formatDateTime(entry.created_at)),
    el("td", {}, entry.actor_email || "—"),
    el("td", {}, entry.target_type === "candidate" && entry.candidate_name
      ? [describeAction({ ...entry, target_type: null }), " ",
        el("button", { type: "button", class: "name-link", onclick: () => selectCandidate(entry.target_id) }, entry.candidate_name)]
      : describeAction(entry)),
    fullLog ? el("td", { class: "col-source muted" }, entry.ip || "") : null));
  results.replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "table table-plain" },
    el("caption", { class: "visually-hidden" }, fullLog ? "Audit log" : "Recruitment activity"),
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "When"), el("th", { scope: "col" }, "Who"),
      el("th", { scope: "col" }, "What"), fullLog ? el("th", { scope: "col", class: "col-source" }, "IP address") : null)),
    el("tbody", {}, rows))));
  renderPager($("#activity-pager"), page, pageTo);
}

// --- Users (admins) -----------------------------------------------------------

async function loadUsers() {
  const results = $("#users-results");
  showLoading(results, "Loading users…");
  try {
    const { data } = await api.listUsers();
    renderUsers(data);
  } catch (error) {
    results.replaceChildren(errorBlock(`Users couldn't load. ${messageOf(error)}`, loadUsers));
  } finally {
    doneLoading(results);
  }
}

function renderUsers(users) {
  const rows = users.map((user) => {
    const self = user.id === state.user.id;
    const roleSelect = el("select", {
      "aria-label": `Role for ${user.email}`,
      onchange: (event) => updateUser(user, { role: event.target.value }, event.target),
    }, Object.entries(ROLE_LABELS).map(([value, label]) => el("option", { value }, label)));
    roleSelect.value = user.role;
    const resetError = el("p", { class: "field-error", role: "alert" });
    const resetInput = el("input", { type: "password", minlength: "12", maxlength: "128", autocomplete: "new-password",
      placeholder: "New password", "aria-label": `New password for ${user.email}` });
    const resetForm = el("form", { class: "inline-form", hidden: true, onsubmit: (event) => {
      event.preventDefault();
      resetPassword(user, resetInput, resetForm, resetError);
    } }, resetInput, el("button", { type: "submit", class: "btn" }, "Set"));
    return el("tr", { class: user.is_active ? null : "is-inactive" },
      el("td", { class: "col-name" }, el("strong", {}, user.email), self ? el("span", { class: "badge badge-check" }, "You") : null,
        el("div", { class: "sub" }, `Created ${formatDate(user.created_at)}`)),
      el("td", {}, roleSelect),
      el("td", {}, user.is_active ? el("span", { class: "status-item ok" }, el("span", { class: "dot" }), "Active")
        : el("span", { class: "status-item warn" }, el("span", { class: "dot" }), "Deactivated")),
      el("td", { class: "col-applied muted" }, user.last_login_at ? formatDateTime(user.last_login_at) : "Never"),
      el("td", { class: "col-actions" },
        el("div", { class: "row-actions" },
          el("button", { type: "button", class: "btn btn-small", onclick: (e) => updateUser(user, { is_active: !user.is_active }, e.currentTarget) },
            user.is_active ? "Deactivate" : "Activate"),
          el("button", { type: "button", class: "btn btn-small btn-ghost", onclick: () => { resetForm.hidden = !resetForm.hidden; if (!resetForm.hidden) resetInput.focus(); } },
            "Reset password")),
        resetForm, resetError));
  });
  $("#users-results").replaceChildren(el("div", { class: "table-wrap" }, el("table", { class: "table table-plain" },
    el("caption", { class: "visually-hidden" }, "User accounts"),
    el("thead", {}, el("tr", {}, ["Account", "Role", "Status", "Last sign-in", "Actions"].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, rows))));
}

async function updateUser(user, changes, control) {
  control.disabled = true;
  try {
    const { data } = await api.updateUser(user.id, changes);
    if ("role" in changes) toast(`${data.email} is now ${ROLE_LABELS[data.role].toLowerCase()}.`, "success");
    else toast(`${data.email} ${data.is_active ? "activated" : "deactivated and signed out"}.`, "success");
    if (data.id === state.user.id) {
      location.reload(); // own role changed: reload with the new permissions
      return;
    }
  } catch (error) {
    toast(messageOf(error), "error");
  }
  loadUsers();
}

async function resetPassword(user, input, form, errorNode) {
  errorNode.textContent = "";
  for (const control of form.elements) control.disabled = true;
  try {
    await api.resetPassword(user.id, input.value);
    toast(`Password reset for ${user.email}. They have been signed out.`, "success");
    form.hidden = true;
    input.value = "";
  } catch (error) {
    errorNode.textContent = messageOf(error);
  } finally {
    for (const control of form.elements) control.disabled = false;
  }
}

async function createUser(event) {
  event.preventDefault();
  const errorNode = $("#user-create-error");
  const form = event.currentTarget;
  errorNode.textContent = "";
  for (const control of form.elements) control.disabled = true;
  try {
    const { data } = await api.createUser({ email: $("#new-email").value.trim(), role: $("#new-role").value, password: $("#new-password").value });
    toast(`Created ${data.email} as ${ROLE_LABELS[data.role].toLowerCase()}.`, "success");
    form.reset();
    loadUsers();
  } catch (error) {
    errorNode.textContent = messageOf(error);
  } finally {
    for (const control of form.elements) control.disabled = false;
  }
}

// --- Session ------------------------------------------------------------------

function goToSignIn(reason) {
  if (signingOut) return;
  signingOut = true;
  const next = encodeURIComponent(location.pathname + location.search);
  location.replace(`/login?${reason}=1&next=${next}`);
}

function sessionEnded(message) {
  if (signingOut) return;
  showBanner(`${message} Taking you to the sign-in page…`);
  setTimeout(() => goToSignIn("expired"), 1500);
}

async function signOut() {
  const button = $("#logout");
  button.disabled = true;
  try {
    await api.logout();
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) {
      button.disabled = false;
      toast(`Couldn't sign out. ${messageOf(error)}`, "error");
      return;
    }
  }
  signingOut = true;
  location.replace("/login?signed_out=1");
}

function openPasswordDialog() {
  const dialog = $("#password-dialog");
  $("#password-form").reset();
  $("#pw-error").textContent = "";
  dialog.showModal();
  $("#pw-current").focus();
}

async function changePassword(event) {
  event.preventDefault();
  const errorNode = $("#pw-error");
  if ($("#pw-new").value !== $("#pw-repeat").value) {
    errorNode.textContent = "The new passwords don't match.";
    return;
  }
  const submit = $("#pw-submit");
  submit.disabled = true;
  try {
    await api.changePassword($("#pw-current").value, $("#pw-new").value);
    $("#password-dialog").close();
    toast("Password changed. Your other sessions were signed out.", "success");
  } catch (error) {
    errorNode.textContent = messageOf(error);
  } finally {
    submit.disabled = false;
  }
}

function renderUser() {
  $("#user-email").textContent = state.user.email;
  const role = $("#user-role");
  role.textContent = ROLE_LABELS[state.user.role] || state.user.role;
  role.className = `role-badge role-${state.user.role}`;
  for (const tab of document.querySelectorAll(".tab")) tab.hidden = !can(VIEW_PERMISSION[tab.dataset.view]);
}

// --- Wiring -----------------------------------------------------------------

function bindEvents() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showView(tab.dataset.view));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const views = allowedViews();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = views[(views.indexOf(tab.dataset.view) + step + views.length) % views.length];
      showView(next);
      $(`#tab-${next}`).focus();
    });
  }

  $("#browse-filters").addEventListener("submit", (event) => event.preventDefault());
  $("#f-q").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => setBrowse({ q: event.target.value.trim() }, { resetOffset: true }), 300);
  });
  for (const key of ["role", "source", "tag", "shortlisted", "sort"]) {
    $(`#f-${key}`).addEventListener("change", (event) => setBrowse({ [key]: event.target.value }, { resetOffset: true }));
  }
  $("#f-clear").addEventListener("click", clearFilters);

  const skillInput = $("#rank-skill");
  skillInput.addEventListener("keydown", (event) => {
    if (event.key === "," || (event.key === "Enter" && skillInput.value.trim())) {
      event.preventDefault();
      if (commitRankInput()) {
        state.rank.offset = 0;
        loadRank();
      }
    } else if (event.key === "Backspace" && !skillInput.value && state.rank.skills.length) {
      removeRankSkill(state.rank.skills[state.rank.skills.length - 1]);
    }
  });
  $("#rank-chip-box").addEventListener("click", (event) => { if (!event.target.closest("button")) skillInput.focus(); });
  $("#rank-form").addEventListener("submit", (event) => {
    event.preventDefault();
    commitRankInput();
    if (!state.rank.skills.length) {
      toast("Add at least one skill to rank by.", "error");
      skillInput.focus();
      return;
    }
    state.rank.offset = 0;
    loadRank();
  });
  for (const key of ["role", "source"]) {
    $(`#r-${key}`).addEventListener("change", (event) => {
      state.rank[key] = event.target.value;
      state.rank.offset = 0;
      loadRank();
    });
  }

  $("#backdrop").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (event) => {
    const inField = event.target instanceof Element && event.target.closest("input, select");
    if (event.key === "Escape" && state.selectedId && !inField && !$("#password-dialog").open) closeDetail();
  });

  $("#logout").addEventListener("click", signOut);
  $("#change-password").addEventListener("click", openPasswordDialog);
  $("#password-form").addEventListener("submit", changePassword);
  $("#pw-cancel").addEventListener("click", () => $("#password-dialog").close());
  $("#user-create").addEventListener("submit", createUser);
}

async function init() {
  onUnauthorized(sessionEnded);
  try {
    const { data } = await api.me();
    state.user = data.user;
    state.permissions = new Set(data.permissions);
    setCsrfToken(data.csrf_token);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return; // onUnauthorized is redirecting
    showBanner(`Couldn't load your account. ${messageOf(error)}`);
    return;
  }
  renderUser();
  fillStaticSelect($("#f-sort"), SORT_OPTIONS, state.browse.sort);
  fillStaticSelect($("#f-shortlisted"), SHORTLIST_OPTIONS, state.browse.shortlisted);
  for (const id of ["#f-role", "#f-source", "#f-tag", "#r-role", "#r-source"]) fillSelect($(id), "All", [], "");
  syncBrowseControls();
  renderRankChips();
  bindEvents();
  loadHealth();
  if (can("candidates:read")) loadFacets();
  showView(state.view);
}

init();
