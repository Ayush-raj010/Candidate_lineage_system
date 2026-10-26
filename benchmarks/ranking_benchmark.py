"""Benchmark skill-overlap ranking on synthetic candidates.

Usage: python -m benchmarks.ranking_benchmark [--count 100000] [--db PATH] [--runs 7]

Builds (or reuses) a SQLite database of synthetic candidates through the
real importer, then times each ranking strategy on the same queries:

  naive   Python, score every candidate                 O(C*s + C log C)
  index   Python inverted index (build timed separately) O(P + M log k)
  sql     repository.rank_candidates on a fresh connection, as each API request does
          (a long-reused connection measured ~25% slower; cause not determined)
  api     GET /api/rank through FastAPI's test client

All strategies must return the same top results, or the script exits 1.
"""

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import repository
from app.db import connect, init_schema
from app.importer import import_records, skill_key
from app.main import create_app
from app.ranking import SkillIndex, rank_naive
from benchmarks.synthetic import generate

TOP_K = 20
QUERIES = [
    ("1 rare skill", ["Skill 150"], None),
    ("1 common skill", ["Next.js"], None),
    ("3 common skills", ["Next.js", "Terraform", "Docker"], None),
    ("5 mixed skills", ["Python", "FastAPI", "PostgreSQL", "Docker", "Skill 120"], None),
    ("10 mixed skills", ["Python", "Go", "Kafka", "AWS", "React", "Redis", "Swift", "GCP", "Skill 090", "Skill 180"], None),
    ("3 common + role filter", ["Next.js", "Terraform", "Docker"], "ML Engineer"),
]


def timed(fn, runs):
    fn()  # warm-up
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        result = fn()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples) * 1000, result


def build_database(path, records):
    conn = connect(path)
    init_schema(conn)
    existing = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    if existing == len(records):
        print(f"Reusing {path} ({existing} candidates)")
    else:
        if existing:
            sys.exit(f"{path} has {existing} candidates, expected {len(records)}; use another --db")
        start = time.perf_counter()
        import_records(conn, records)
        print(f"Imported {len(records)} candidates in {time.perf_counter() - start:.1f} s")
    return conn


def rank_fresh(db_path, skills, role):
    conn = connect(db_path)
    try:
        return repository.rank_candidates(conn, skills, role=role, limit=TOP_K)
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=100_000)
    parser.add_argument("--db", type=Path, default=None, help="reuse/create this database (default: temp file)")
    parser.add_argument("--runs", type=int, default=7)
    args = parser.parse_args(argv)

    records = generate(args.count)
    db_path = args.db or Path(tempfile.mkdtemp()) / "ranking_benchmark.db"
    conn = build_database(db_path, records)
    links = conn.execute("SELECT COUNT(*) FROM candidate_skills").fetchone()[0]
    print(f"C = {len(records)} candidates, {links} candidate-skill links (s = {links / len(records):.2f})")

    start = time.perf_counter()
    index = SkillIndex(records)
    print(f"Python index build: {(time.perf_counter() - start) * 1000:.0f} ms (once)\n")

    print(f"Median of {args.runs} runs, top {TOP_K}, milliseconds:\n")
    print("| Query | P | M | naive | index | sql | api |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    mismatches = 0
    with TestClient(create_app(db_path)) as client:
        for label, skills, role in QUERIES:
            subset = [r for r in records if role is None or r["target_role"] == role]
            sub_index = index if role is None else SkillIndex(subset)
            P = sum(len(sub_index.postings.get(skill_key(s), ())) for s in skills)

            naive_ms, naive = timed(lambda: rank_naive(subset, skills, TOP_K), args.runs)
            index_ms, indexed = timed(lambda: sub_index.rank(skills, TOP_K), args.runs)
            sql_ms, sql = timed(lambda: rank_fresh(db_path, skills, role), args.runs)
            params = {"skills": skills, "limit": TOP_K, **({"role": role} if role else {})}
            api_ms, api = timed(lambda: client.get("/api/rank", params=params).json(), args.runs)

            sql_top = [(i["candidate"]["id"], i["score"]) for i in sql["items"]]
            api_top = [(i["candidate"]["id"], i["score"]) for i in api["items"]]
            if not (naive == indexed == sql_top == api_top):
                mismatches += 1
                print(f"MISMATCH for {label}")
            print(f"| {label} | {P:,} | {sql['total']:,} | {naive_ms:.1f} | {index_ms:.1f} | {sql_ms:.1f} | {api_ms:.1f} |")
    conn.close()

    print("\nAll strategies returned identical top results." if not mismatches else f"\n{mismatches} mismatches")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
