"""Import candidates.json into SQLite without touching recruiter data.

Each valid record is stored verbatim as an append-only revision and
mirrored into the queryable `candidates` and `candidate_skills` tables.
Tags, shortlist entries and AI summaries are never written here, and
candidates are never deleted.

Usage: python -m app.importer [--json PATH] [--db PATH] [--dry-run]
"""

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from app.db import BASE_DIR, DEFAULT_DB_PATH, LegacySchemaError, connect, init_schema

DEFAULT_JSON_PATH = BASE_DIR / "candidates.json"
REQUIRED_TEXT_FIELDS = ("id", "name", "target_role", "source")
_MISSING = object()


class ValidationError(ValueError):
    """A record that cannot be imported as-is."""


@dataclass
class CandidateRow:
    id: str
    name: str
    target_role: str
    years_experience: int | None
    source: str
    notes: str
    applied_date: str | None
    skills: list[str]
    source_json: str
    content_hash: str


@dataclass
class ImportReport:
    total_in_file: int = 0
    inserted: list[str] = field(default_factory=list)
    updated: dict[str, list[str]] = field(default_factory=dict)  # id -> changed fields
    unchanged: list[str] = field(default_factory=list)
    missing_from_file: list[str] = field(default_factory=list)
    invalid: list[tuple[int, str | None, str]] = field(default_factory=list)  # (index, id, reason)
    dry_run: bool = False

    def summary(self):
        mode = " (dry run, nothing written)" if self.dry_run else ""
        lines = [
            f"Import of {self.total_in_file} records{mode}",
            f"  inserted:          {len(self.inserted)}",
            f"  updated:           {len(self.updated)}",
            f"  unchanged:         {len(self.unchanged)}",
            f"  missing from file: {len(self.missing_from_file)} (kept, not deleted)",
            f"  invalid:           {len(self.invalid)} (skipped)",
        ]
        lines += [f"  updated {cid}: {', '.join(fields)}" for cid, fields in self.updated.items()]
        lines += [f"  missing {cid}" for cid in self.missing_from_file]
        lines += [f"  invalid record #{i} ({cid or 'no id'}): {reason}" for i, cid, reason in self.invalid]
        return "\n".join(lines)


def skill_key(skill):
    return " ".join(skill.split()).casefold()


def canonical_json(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _require_text(raw, key):
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"'{key}' must be a non-empty string")
    return value.strip()


def validate_record(raw):
    """Return a CandidateRow, or raise ValidationError.

    Missing optional values stay None; nothing is filled in with defaults.
    """
    if not isinstance(raw, dict):
        raise ValidationError("record must be a JSON object")
    candidate_id, name, target_role, source = (_require_text(raw, key) for key in REQUIRED_TEXT_FIELDS)

    notes = raw.get("notes")
    if not isinstance(notes, str):
        raise ValidationError("'notes' must be a string (use \"\" when there are none)")

    years = raw.get("years_experience")
    if isinstance(years, float) and years.is_integer():
        years = int(years)
    if years is not None and (isinstance(years, bool) or not isinstance(years, int) or years < 0):
        raise ValidationError("'years_experience' must be a non-negative whole number or null")

    raw_skills = raw.get("skills")
    if not isinstance(raw_skills, list):
        raise ValidationError("'skills' must be a list")
    skills, seen = [], set()
    for skill in raw_skills:
        if not isinstance(skill, str) or not skill.strip():
            raise ValidationError("every skill must be a non-empty string")
        if skill_key(skill) not in seen:
            seen.add(skill_key(skill))
            skills.append(" ".join(skill.split()))

    applied = raw.get("applied_date")
    if applied is not None:
        try:
            valid_date = isinstance(applied, str) and date.fromisoformat(applied).isoformat() == applied
        except ValueError:
            valid_date = False
        if not valid_date:
            raise ValidationError("'applied_date' must be a YYYY-MM-DD date or null")

    source_json = canonical_json(raw)
    return CandidateRow(
        id=candidate_id,
        name=name,
        target_role=target_role,
        years_experience=years,
        source=source,
        notes=notes,
        applied_date=applied,
        skills=skills,
        source_json=source_json,
        content_hash=hashlib.sha256(source_json.encode("utf-8")).hexdigest(),
    )


def load_records(json_path):
    with Path(json_path).open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError(f"{json_path} must contain a JSON array of candidate records")
    return data


def import_records(conn, records, *, dry_run=False):
    """Validate and apply records in one transaction; roll back if dry_run."""
    report = ImportReport(total_in_file=len(records), dry_run=dry_run)
    valid, file_ids = [], set()
    for index, raw in enumerate(records):
        raw_id = raw.get("id") if isinstance(raw, dict) else None
        try:
            row = validate_record(raw)
            if row.id in file_ids:
                raise ValidationError(f"duplicate id '{row.id}' in file")
        except ValidationError as exc:
            report.invalid.append((index, raw_id if isinstance(raw_id, str) else None, str(exc)))
            continue
        file_ids.add(row.id)
        valid.append(row)
    # A known id whose record is invalid is reported as invalid, not as missing.
    file_ids |= {cid.strip() for _, cid, _ in report.invalid if cid}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in valid:
            _apply_row(conn, row, now, report)
        report.missing_from_file = sorted(
            r["id"] for r in conn.execute("SELECT id FROM candidates") if r["id"] not in file_ids
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if dry_run else "COMMIT")
    return report


def _apply_row(conn, row, now, report):
    current = conn.execute("SELECT content_hash FROM candidates WHERE id = ?", (row.id,)).fetchone()
    if current and current["content_hash"] == row.content_hash:
        report.unchanged.append(row.id)
        return

    # A record reverted to an earlier version reuses that revision. ON CONFLICT
    # (not OR IGNORE) so only the duplicate key is skipped, never a CHECK failure.
    conn.execute(
        "INSERT INTO candidate_revisions (candidate_id, content_hash, source_json, imported_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (candidate_id, content_hash) DO NOTHING",
        (row.id, row.content_hash, row.source_json, now),
    )
    fields = (row.name, row.target_role, row.years_experience, row.source, row.notes,
              row.applied_date, row.content_hash)
    if current is None:
        conn.execute(
            "INSERT INTO candidates (name, target_role, years_experience, source, notes, applied_date, "
            "content_hash, imported_at, updated_at, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            fields + (now, now, row.id),
        )
        report.inserted.append(row.id)
    else:
        previous = conn.execute(
            "SELECT source_json FROM candidate_revisions WHERE candidate_id = ? AND content_hash = ?",
            (row.id, current["content_hash"]),
        ).fetchone()
        report.updated[row.id] = _changed_fields(json.loads(previous["source_json"]), json.loads(row.source_json))
        conn.execute(
            "UPDATE candidates SET name = ?, target_role = ?, years_experience = ?, source = ?, notes = ?, "
            "applied_date = ?, content_hash = ?, updated_at = ? WHERE id = ?",
            fields + (now, row.id),
        )
        conn.execute("DELETE FROM candidate_skills WHERE candidate_id = ?", (row.id,))

    for skill in row.skills:
        conn.execute(
            "INSERT INTO skills (name, name_key) VALUES (?, ?) ON CONFLICT (name_key) DO NOTHING",
            (skill, skill_key(skill)),
        )
        conn.execute(
            "INSERT INTO candidate_skills (candidate_id, skill_id) SELECT ?, id FROM skills WHERE name_key = ?",
            (row.id, skill_key(skill)),
        )


def _changed_fields(old, new):
    return sorted(key for key in old.keys() | new.keys() if old.get(key, _MISSING) != new.get(key, _MISSING))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import candidates.json into the SQLite database.")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON_PATH, help="candidate dataset (JSON array)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database file")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    args = parser.parse_args(argv)

    try:
        records = load_records(args.json)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    # A dry run against a database that doesn't exist yet must not create the file.
    conn = connect(":memory:" if args.dry_run and not args.db.exists() else args.db)
    try:
        init_schema(conn)
        report = import_records(conn, records, dry_run=args.dry_run)
    except LegacySchemaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print(report.summary())
    return 1 if report.invalid else 0


if __name__ == "__main__":
    sys.exit(main())
