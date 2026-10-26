"""SQL for the candidate API.

Every value from a request is bound as a parameter. The only SQL text that
depends on a request is the ORDER BY clause, which is looked up in SORT_SQL.

Recruiter writes touch only candidate_tags and shortlist. Each is a single
statement, so it is atomic on the autocommit connection.
"""

from datetime import datetime, timezone

from app.importer import skill_key

SORT_SQL = {
    "-applied_date": "c.applied_date DESC NULLS LAST",
    "applied_date": "c.applied_date ASC NULLS LAST",
    "name": "c.name COLLATE NOCASE ASC",
    "-name": "c.name COLLATE NOCASE DESC",
    "-years_experience": "c.years_experience DESC NULLS LAST",
    "years_experience": "c.years_experience ASC NULLS LAST",
}

_LIKE = "LIKE ? ESCAPE '\\'"

_CANDIDATE_COLUMNS = """
    c.id, c.name, c.target_role, c.years_experience, c.source, c.applied_date,
    EXISTS (SELECT 1 FROM shortlist sl WHERE sl.candidate_id = c.id) AS shortlisted
"""


def _escape_like(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _where(q, role, source, tag, shortlisted):
    """Build the WHERE clause. Every search word must match somewhere."""
    clauses, params = [], []
    for term in (q or "").split():
        clauses.append(
            f"(c.name {_LIKE} OR c.target_role {_LIKE} OR c.notes {_LIKE} OR EXISTS ("
            f"SELECT 1 FROM candidate_skills cs JOIN skills s ON s.id = cs.skill_id "
            f"WHERE cs.candidate_id = c.id AND s.name {_LIKE}))"
        )
        params += [f"%{_escape_like(term)}%"] * 4
    if role is not None:
        clauses.append("c.target_role = ?")
        params.append(role)
    if source is not None:
        clauses.append("c.source = ?")
        params.append(source)
    if tag is not None:
        clauses.append("EXISTS (SELECT 1 FROM candidate_tags t WHERE t.candidate_id = c.id AND t.tag = ?)")
        params.append(tag)
    if shortlisted is not None:
        negate = "" if shortlisted else "NOT "
        clauses.append(f"{negate}EXISTS (SELECT 1 FROM shortlist sl WHERE sl.candidate_id = c.id)")
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def list_candidates(conn, *, q=None, role=None, source=None, tag=None, shortlisted=None,
                    sort="-applied_date", limit=50, offset=0):
    """Return (page of candidates, total matching)."""
    order = SORT_SQL[sort]
    where, params = _where(q, role, source, tag, shortlisted)
    total = conn.execute(f"SELECT COUNT(*) FROM candidates c{where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_CANDIDATE_COLUMNS} FROM candidates c{where} ORDER BY {order}, c.id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return _attach_skills_and_tags(conn, [dict(row) for row in rows]), total


def get_candidate(conn, candidate_id):
    row = conn.execute(
        f"SELECT {_CANDIDATE_COLUMNS}, c.notes FROM candidates c WHERE c.id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        return None
    return _attach_skills_and_tags(conn, [dict(row)])[0]


def rank_candidates(conn, skills, *, role=None, source=None, limit=20, offset=0):
    """Rank candidates by how many of `skills` they have. Same order as app.ranking.

    Two query shapes, chosen by whether a filter is present:
    - no filter: read only the required skills' entries in ix_cs_skill and
      count per candidate, so cost grows with how common the skills are,
      not with the number of candidates;
    - role/source filter: take the filtered candidates from their index and
      score each with q lookups on the candidate_skills primary key, instead
      of scoring every match and discarding most of them afterwards.
    """
    keys = [skill_key(skill) for skill in skills]
    placeholders = ", ".join("?" * len(keys))
    known = {
        row["name_key"]: (row["id"], row["name"])
        for row in conn.execute(f"SELECT id, name, name_key FROM skills WHERE name_key IN ({placeholders})", keys)
    }
    query_skills = [known[key][1] for key in keys if key in known]
    result = {
        "query_skills": query_skills,
        "unknown_skills": [skill for skill, key in zip(skills, keys) if key not in known],
        "items": [],
        "total": 0,
    }
    if not query_skills:
        return result

    skill_ids = [known[key][0] for key in keys if key in known]
    filters, params = [], []
    if role is not None:
        filters.append("c.target_role = ?")
        params.append(role)
    if source is not None:
        filters.append("c.source = ?")
        params.append(source)
    skill_placeholders = ", ".join("?" * len(skill_ids))
    if filters:
        matches = (
            "WITH matches AS (SELECT c.id AS candidate_id, (SELECT COUNT(*) FROM candidate_skills cs "
            f"WHERE cs.candidate_id = c.id AND cs.skill_id IN ({skill_placeholders})) AS score "
            f"FROM candidates c WHERE {' AND '.join(filters)}) "
        )
        matched = " WHERE m.score > 0"
    else:
        matches = (
            "WITH matches AS (SELECT candidate_id, COUNT(*) AS score FROM candidate_skills "
            f"WHERE skill_id IN ({skill_placeholders}) GROUP BY candidate_id) "
        )
        matched = ""
    params = [*skill_ids, *params]  # placeholders appear in this order in both shapes
    result["total"] = conn.execute(f"{matches}SELECT COUNT(*) FROM matches m{matched}", params).fetchone()[0]
    rows = conn.execute(
        f"{matches}SELECT {_CANDIDATE_COLUMNS}, m.score FROM matches m JOIN candidates c ON c.id = m.candidate_id"
        f"{matched} ORDER BY m.score DESC, c.years_experience DESC NULLS LAST, c.id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    candidates = [dict(row) for row in rows]
    scores = [candidate.pop("score") for candidate in candidates]
    _attach_skills_and_tags(conn, candidates)
    for candidate, score in zip(candidates, scores):
        has = {skill_key(skill) for skill in candidate["skills"]}
        matched = [name for name in query_skills if skill_key(name) in has]
        result["items"].append({
            "candidate": candidate,
            "score": score,
            "coverage": round(score / len(query_skills), 4),
            "matched_skills": matched,
            "missing_skills": [name for name in query_skills if name not in matched],
        })
    return result


def _attach_skills_and_tags(conn, candidates):
    """Add skills and tags with one query each for the whole page."""
    by_id = {candidate["id"]: candidate for candidate in candidates}
    for candidate in candidates:
        candidate.update(skills=[], tags=[], shortlisted=bool(candidate["shortlisted"]))
    if not by_id:
        return candidates
    placeholders = ", ".join("?" * len(by_id))
    for row in conn.execute(
        "SELECT cs.candidate_id, s.name FROM candidate_skills cs JOIN skills s ON s.id = cs.skill_id "
        f"WHERE cs.candidate_id IN ({placeholders}) ORDER BY s.name COLLATE NOCASE",
        list(by_id),
    ):
        by_id[row["candidate_id"]]["skills"].append(row["name"])
    for row in conn.execute(
        f"SELECT candidate_id, tag FROM candidate_tags WHERE candidate_id IN ({placeholders}) ORDER BY tag",
        list(by_id),
    ):
        by_id[row["candidate_id"]]["tags"].append(row["tag"])
    return candidates


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def candidate_exists(conn, candidate_id):
    return conn.execute("SELECT 1 FROM candidates WHERE id = ?", (candidate_id,)).fetchone() is not None


def get_tags(conn, candidate_id):
    return [row[0] for row in conn.execute(
        "SELECT tag FROM candidate_tags WHERE candidate_id = ? ORDER BY tag", (candidate_id,)
    )]


def add_tag(conn, candidate_id, tag):
    """Return True if the tag was added, False if the candidate already had it."""
    cursor = conn.execute(
        "INSERT INTO candidate_tags (candidate_id, tag, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT (candidate_id, tag) DO NOTHING",
        (candidate_id, tag, _utc_now()),
    )
    return cursor.rowcount == 1


def remove_tag(conn, candidate_id, tag):
    """Return True if the tag was removed, False if the candidate didn't have it."""
    cursor = conn.execute("DELETE FROM candidate_tags WHERE candidate_id = ? AND tag = ?", (candidate_id, tag))
    return cursor.rowcount == 1


def is_shortlisted(conn, candidate_id):
    return conn.execute("SELECT 1 FROM shortlist WHERE candidate_id = ?", (candidate_id,)).fetchone() is not None


def set_shortlisted(conn, candidate_id, shortlisted):
    """Shortlist or un-shortlist. Re-shortlisting keeps the original timestamp."""
    if shortlisted:
        conn.execute(
            "INSERT INTO shortlist (candidate_id, shortlisted_at) VALUES (?, ?) "
            "ON CONFLICT (candidate_id) DO NOTHING",
            (candidate_id, _utc_now()),
        )
    else:
        conn.execute("DELETE FROM shortlist WHERE candidate_id = ?", (candidate_id,))
    row = conn.execute("SELECT shortlisted_at FROM shortlist WHERE candidate_id = ?", (candidate_id,)).fetchone()
    return {"id": candidate_id, "shortlisted": row is not None, "shortlisted_at": row[0] if row else None}


def facets(conn):
    """Counts for each filter value, across all candidates."""

    def counts(sql):
        return [{"value": row[0], "count": row[1]} for row in conn.execute(sql)]

    return {
        "total": conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
        "shortlisted": conn.execute("SELECT COUNT(*) FROM shortlist").fetchone()[0],
        "roles": counts("SELECT target_role, COUNT(*) AS n FROM candidates GROUP BY target_role ORDER BY n DESC, target_role"),
        "sources": counts("SELECT source, COUNT(*) AS n FROM candidates GROUP BY source ORDER BY n DESC, source"),
        "skills": counts(
            "SELECT s.name, COUNT(*) AS n FROM candidate_skills cs JOIN skills s ON s.id = cs.skill_id "
            "GROUP BY s.id ORDER BY n DESC, s.name COLLATE NOCASE"
        ),
        "tags": counts("SELECT tag, COUNT(*) AS n FROM candidate_tags GROUP BY tag ORDER BY n DESC, tag"),
    }
