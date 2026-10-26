-- Candidate triage schema.
--
-- Ownership:
--   importer  -> candidate_revisions, candidates, skills, candidate_skills
--   recruiter -> candidate_tags, shortlist          (importer never writes these)
--   AI layer  -> ai_summaries                       (keyed to an exact revision)

-- Original records, append-only: never updated or deleted.
CREATE TABLE IF NOT EXISTS candidate_revisions (
    candidate_id  TEXT NOT NULL,
    content_hash  TEXT NOT NULL,          -- sha256 of source_json
    source_json   TEXT NOT NULL CHECK (json_valid(source_json)),
    imported_at   TEXT NOT NULL,
    PRIMARY KEY (candidate_id, content_hash)
);

-- Queryable view of each candidate's current revision.
CREATE TABLE IF NOT EXISTS candidates (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL CHECK (length(trim(name)) > 0),
    target_role      TEXT NOT NULL,
    years_experience INTEGER CHECK (years_experience IS NULL OR years_experience >= 0),  -- NULL = not stated
    source           TEXT NOT NULL,
    notes            TEXT NOT NULL,       -- '' allowed, never invented
    applied_date     TEXT CHECK (applied_date IS NULL OR date(applied_date) = applied_date),
    content_hash     TEXT NOT NULL,
    imported_at      TEXT NOT NULL,       -- first import
    updated_at       TEXT NOT NULL,       -- last change from the source file
    FOREIGN KEY (id, content_hash) REFERENCES candidate_revisions (candidate_id, content_hash)
);

CREATE TABLE IF NOT EXISTS skills (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,               -- display form, e.g. "Next.js"
    name_key TEXT NOT NULL UNIQUE         -- whitespace-collapsed, casefolded
);

CREATE TABLE IF NOT EXISTS candidate_skills (
    candidate_id TEXT    NOT NULL REFERENCES candidates (id) ON DELETE CASCADE,
    skill_id     INTEGER NOT NULL REFERENCES skills (id),
    PRIMARY KEY (candidate_id, skill_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS candidate_tags (
    candidate_id TEXT NOT NULL REFERENCES candidates (id) ON DELETE RESTRICT,
    tag          TEXT NOT NULL CHECK (tag = lower(trim(tag)) AND length(tag) BETWEEN 1 AND 40),
    created_at   TEXT NOT NULL,
    PRIMARY KEY (candidate_id, tag)
) WITHOUT ROWID;

-- A row exists only while the candidate is shortlisted.
CREATE TABLE IF NOT EXISTS shortlist (
    candidate_id   TEXT PRIMARY KEY REFERENCES candidates (id) ON DELETE RESTRICT,
    shortlisted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_summaries (
    cache_key      TEXT PRIMARY KEY,      -- sha256(content_hash, requirements, prompt_version, model)
    candidate_id   TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    requirements   TEXT NOT NULL DEFAULT '',  -- normalized ranking skills; '' = target role only
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    result_json    TEXT NOT NULL CHECK (json_valid(result_json)),
    created_at     TEXT NOT NULL,
    FOREIGN KEY (candidate_id, content_hash) REFERENCES candidate_revisions (candidate_id, content_hash)
);

CREATE INDEX IF NOT EXISTS ix_candidates_role   ON candidates (target_role);
CREATE INDEX IF NOT EXISTS ix_candidates_source ON candidates (source);
CREATE INDEX IF NOT EXISTS ix_candidates_date   ON candidates (applied_date);
CREATE INDEX IF NOT EXISTS ix_cs_skill          ON candidate_skills (skill_id, candidate_id);  -- inverted index for ranking
CREATE INDEX IF NOT EXISTS ix_tags_tag          ON candidate_tags (tag);
CREATE INDEX IF NOT EXISTS ix_ai_candidate      ON ai_summaries (candidate_id);
