# AI log

How AI was used to build this project. It covers **one conversation** with Claude Code (Anthropic's
coding agent, VS Code extension), 24–27 October 2026. Model: Claude Opus 5.5, except one
verification turn on Claude Sonnet 5.5.

Only what happened in that conversation is recorded here. Work done with other tools, or by hand,
is listed under [Not covered](#not-covered). Test counts and timings are the ones reported at the
time.

## How I worked with the AI

- **Stage by stage, with review between stages.** Each stage was a separate prompt. I asked for a
  plan before any code, reviewed it, then asked for the next stage. The AI was told not to modify
  files in planning turns, and it didn't.
- **Verification on every stage.** I asked for actual test results every time and for
  "don't claim success without verification". The AI also checked its own tests: it deliberately
  broke a feature at runtime and confirmed the tests then failed ("mutation checks").
- **Decisions stayed with me.** The AI recommended; I chose (see [Decisions](#decisions-i-made)).

## Timeline

| # | Stage | Result |
|---|---|---|
| 1 | Inspect workspace, plan architecture (no code) | Data profile and plan; brief and buggy JS function not in the workspace |
| 2 | Verify my `init_db.py` import against the JSON | Data matched, but `applied_date` was dropped; schema redesigned |
| 3 | Schema, importer, `--dry-run`, tests | 49 tests passing; database rebuilt (48 inserted, re-import 48 unchanged) |
| 4 | Re-verify the import (Sonnet 5.5) | 49 passing; all 48 records and `applied_date` match |
| 5 | Plan the FastAPI backend (asked twice) | Second pass found a real `INSERT OR IGNORE` problem |
| 6 | Dependencies, app skeleton, `/api/health` | 64 passing |
| 7 | Search, filters, detail, facets | 184 passing |
| 8 | Tags and shortlist | 250 passing |
| 9 | Skill ranking and 100k benchmark | 337 passing; worst case 194 ms through the API |
| 10 | AI summaries (Groq, `openai/gpt-oss-120b`) | 401 passing; 22 real model calls |
| 11 | Dashboard frontend | 416 Python + 32 JS tests; 46 browser checks |
| 12 | Authentication and role-based access | 533 Python + 43 JS tests; 40 + 47 browser checks |
| 13 | Mermaid architecture diagram in README | Added; not rendered or checked by the AI |
| 14 | This log | — |

## Stage notes

### 1. Planning

- **Workspace contents:** the AI found only `README.md` and `candidates.json`. It profiled the
  data instead of assuming a schema: 48 records, one consistent shape, templated notes, notes that
  mention skills missing from the `skills` list in 12 records, and referral notes that disagree
  with `source` in 2.
- **Missing inputs:** the case-study brief and the buggy JavaScript function weren't in the
  workspace. The AI asked for them rather than inventing them. They were never provided in this
  conversation.
- **Outcome:** an architecture plan (FastAPI, SQLite, vanilla JS, pytest), which I reviewed.

### 2–4. Data layer

- **What the check found:** the AI compared my `init_db.py` output with the JSON row by row. Values
  matched, but `applied_date` had been dropped entirely, years were stored as decimals, missing
  values became misleading defaults (`"Unknown"`, `0`), and re-imports silently ignored changed
  records.
- **Schema:** I approved a new schema with an append-only `candidate_revisions` table, so original
  evidence is never overwritten, and separate tables for recruiter data.
- **Setup issue:** `python` pointed at an unrelated virtualenv, so the AI created a project `.venv`.

### 5–8. Backend API

- **Second planning pass:** reviewing its own plan, the AI found that SQLite's `INSERT OR IGNORE`
  silently skips CHECK-constraint failures, not just duplicates. It switched to
  `ON CONFLICT … DO NOTHING`, including one place in the importer.
- **Dependency warning:** Starlette 1.9 deprecated `httpx` for its test client. The AI checked
  Starlette's source before switching to `httpx2`.
- **Weak tests it fixed:**
  - An assertion ending in `or True`, which could never fail.
  - A duplicate-tag test that would pass even if the row was overwritten, because timestamps are
    stored to the second.
  - A test that forgot results come back newest-first.
- **Mutation checks:** turning off LIKE-wildcard escaping made 4 tests fail. Making tag and
  shortlist inserts overwrite existing rows made 3 fail.

### 9. Ranking

- **Benchmark:** 100,000 synthetic candidates loaded through the real importer, comparing a naive
  scan, an in-memory index, SQL and the full API. All four returned identical top-20 results.
- **Optimisation from profiling:** the first SQL version took 290 ms (3 common skills) and 333 ms
  (with a role filter). After profiling, two changes brought these to 192 ms and 134 ms.
- **Not explained:** a reused connection ran ~25% slower than a fresh one. The cause wasn't found,
  and this is stated in `docs/RANKING.md` rather than hidden.

### 10. AI summaries

The product's own AI feature.

- **Before building:** the AI inspected the installed `openai` 3.37.0 SDK (a major version newer
  than it knew), probed Groq with a trivial prompt to confirm strict JSON-schema support, and read
  only variable *names* from `.env`, never the key.
- **Grounding:** the summary design checks every claim's quoted evidence against the candidate's
  stored record. Contradictions between notes and structured data are detected by code, not left
  to the model.
- **Real model calls:** 22 summary calls plus 3 probe calls (one with a deliberately invalid key),
  run on a scratch copy of the database. The real output showed problems the mocked tests couldn't:
  - The model guessed gender from names ("his", "her").
  - It repeated the code-found contradictions.
  - It listed missing required skills as concerns.

  After the fixes there were no gendered pronouns in 12 runs. That rests on a prompt instruction,
  not a code check.
- **Bugs fixed:** a quote-normalisation bug, and a test that assumed Linux behaviour (Windows times
  out instead of refusing a connection).
- **Key safety:** a final scan found the API key in no file except `.env`.

### 11. Frontend

- **Browser checks:** the AI drove headless Chrome through the DevTools protocol (46 checks plus
  screenshots), using a scratch database and the AI key disabled.
- **Real bugs found:**
  - The Escape-key handler crashed.
  - Static files had no `Cache-Control`, so browsers could run a stale `app.js`.
  - Toast text was unreadable in dark mode.
  - New summaries appeared below the fold.
- **Its own mistakes:** two broken checks in its new test file, and three wrong assumptions in its
  browser script. All fixed without weakening the tests.

### 12. Authentication and roles

- **Breakage from requiring sign-in:** 276 existing tests failed with 401, as expected. Instead of
  bypassing auth, they now sign in through a helper that creates real sessions. No assertion was
  weakened; two changed because the API grew.
- **Mutation checks:** disabling CSRF, idle expiry, the deactivated-user check, permissions,
  throttling or the Origin check each made tests fail.
- **Bugs found in browser testing:**
  - Submitting the sign-in form before its script loaded put the **password in the URL**. Fixed
    with a disabled button and a POST form, and covered by a test.
  - The text "null" appeared in two pagers since stage 11.
- **Bugs found while running the test suite:**
  - My own `uvicorn --reload` dev servers picked up the new code and migrated the real database
    (backed up first, data verified identical). Two servers starting together both ran the
    migration, a race the AI then fixed.
  - The AI's first fix for that race hung, because SQLite can't back up through a connection
    holding a write lock. This was found with a timeout traceback and fixed.
- **One full test run took 61 minutes** instead of about 1. The likely cause is my dev servers'
  reloader using about 7 CPU-hours polling `.venv`, but this was not confirmed. The next run took
  58 seconds.
- **Interruption:** I stopped the AI from running the migration tests three times in a row; it
  continued with a single run.

## Decisions I made

- **Stack and AI provider:** FastAPI, sqlite3, vanilla JS and pytest. For the AI feature, Groq with
  `openai/gpt-oss-120b`, using my own key in `.env`.
- **Data layer:** approved the schema with candidate revisions, rebuilding the database, and keeping
  `init_db.py` as the entry point.
- **Startup:** the app must not import candidates; I run `python init_db.py`.
- **Order of work:** one stage at a time, and the scope and requirements for each, including the
  three roles and their permissions.
- **Open:** the AI chose to give admins **read-only** candidate access. It flagged this for my
  decision; it's a one-line change in `PERMISSIONS`.

## Not covered

- **The original `init_db.py`:** written before stage 2; its origin isn't recorded here.
- **The demo-accounts feature** (`GCCX_DEMO_LOGIN`, `python -m app.manage create-demo`, the list on
  the sign-in page): added after this conversation's last code change and not reviewed or tested
  in it. It uses fixed, published passwords, and when enabled the public `/api/health` returns them.
  The README's "no default account" statement assumes the flag is off.
- **Not done in this conversation:** the buggy JavaScript function review, the engineering note and
  the slides.
- **Any other AI tools or chats.**
