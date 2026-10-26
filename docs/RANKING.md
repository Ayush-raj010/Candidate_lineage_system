# Skill-overlap ranking

## Problem

Given a set of required skills, rank candidates by how many of them they have.
It must stay fast at 100,000 candidates.

**Score** = number of distinct required skills the candidate has (case-insensitive,
whitespace-normalized). **Order:** score high to low → `years_experience` high to low
(unknown last) → `id`. The order is fully deterministic. Candidates with score 0 are not
returned. Required skills that no candidate has are reported back in `unknown_skills`, and
don't lower anyone's `coverage` (= score / recognised required skills).

## Notation

| Symbol | Meaning |
|---|---|
| C | candidates |
| s | average skills per candidate |
| q | required skills in the query (≤ 20) |
| P | total length of the q skills' posting lists = candidate–skill rows that match |
| M | candidates with score ≥ 1 (M ≤ P, M ≤ C) |
| k | results requested (`limit`, ≤ 200) |
| F | candidates passing the role/source filter |

## Approaches

**Naive scan** ([app/ranking.py](../app/ranking.py) `rank_naive`): intersect every candidate's
skills with the query, then sort the scored ones.
Time O(C·s + C log C), space O(C). It touches every candidate whether or not it matches.

**Inverted index** ([app/ranking.py](../app/ranking.py) `SkillIndex`): map each skill to the
candidates who have it. A query walks only the q posting lists, counting hits per candidate
in a hash map, then keeps the top k with a bounded heap (`heapq.nsmallest`).
- Build: O(C·s) time and space, once.
- Query: **O(P + M log k)** time, **O(M)** extra space.
- Cost depends on how common the requested skills are, not on C. A rare skill is close to free.

**SQL, used by the API** ([app/repository.py](../app/repository.py) `rank_candidates`): the same
inverted-index idea, using the database index `ix_cs_skill (skill_id, candidate_id)` as the
posting lists. There are two query shapes:

| Case | Plan (from `EXPLAIN QUERY PLAN`) | Cost |
|---|---|---|
| No filter | `SEARCH candidate_skills USING COVERING INDEX ix_cs_skill (skill_id=?)`, group by candidate (temp B-tree), look up each match by primary key, sort | O(P log P + M log C + M log M) |
| Role/source filter | `SEARCH c USING INDEX ix_candidates_role`, then for each of the F candidates `SEARCH cs USING PRIMARY KEY (candidate_id=? AND skill_id=?)`, sort matches | O(F · q · log(C·s) + M log M) |

The filtered shape exists because the first version scored all M matches and only then applied
the filter. With 3 common skills and a role filter, that took 333 ms; starting from the
filter takes 134 ms. Both shapes are tested against the Python reference, and two tests check
that the plans keep using these indexes.

Each request runs two statements: `total`, then the page. `total` counts the grouped matches
without joining `candidates` when there's no filter (58 ms against 97 ms with the join, for
3 common skills). A single query with a `COUNT(*) OVER ()` window total took 185 ms against
192 ms for the two queries, and returns no total when `offset` is past the end, so I kept
two queries.

## Benchmark

`python -m benchmarks.ranking_benchmark` ([benchmarks/ranking_benchmark.py](../benchmarks/ranking_benchmark.py))
generates synthetic candidates ([benchmarks/synthetic.py](../benchmarks/synthetic.py)) and loads them
through the real importer. Skill popularity is skewed: 200 skills, the most common (Next.js)
on 54% of candidates and the rarest on ~1%, with 3–12 skills each. All four strategies must
return identical top-20 results or the script exits 1.

Run on 2026-10-26: AMD64 Family 23 Model 96 (AuthenticAMD), Windows 11, Python 3.12.7,
SQLite 3.45.3. C = 100,000 candidates, 750,484 candidate–skill rows (s = 7.5). Import took
19.3 s, and building the Python index took 326 ms. Median of 7 runs, top 20, in milliseconds:

| Query | P | M | naive | index | sql | api |
|---|---:|---:|---:|---:|---:|---:|
| 1 rare skill | 1,454 | 1,454 | 231.8 | 1.3 | 12.9 | 17.4 |
| 1 common skill | 54,056 | 54,056 | 311.2 | 44.8 | 87.6 | 92.5 |
| 3 common skills | 119,591 | 77,927 | 336.5 | 78.4 | 191.9 | 194.3 |
| 5 mixed skills | 54,575 | 45,151 | 301.4 | 49.2 | 104.7 | 106.5 |
| 10 mixed skills | 76,353 | 54,634 | 316.9 | 61.3 | 138.4 | 147.0 |
| 3 common + role filter | 11,833 | 7,744 | 35.3 | 6.0 | 134.1 | 135.3 |

All strategies returned identical top results. (P and M in the filtered row are for the
filtered subset; the naive and index columns in that row also run on the pre-filtered subset.)

- `sql` opens a fresh connection per call, as each API request does. `api` is the full HTTP
  round trip through FastAPI's test client.
- The naive scan stays at ~230–340 ms whatever the query, because it always reads all 100k.
  The index-based approaches scale with P: a rare skill takes 1–13 ms.
- Worst case measured through the API: **194 ms**, for 3 skills matching 78% of candidates.

## Trade-offs

- **Why SQL instead of the faster in-memory index?** The in-memory index is 2–4× faster on
  common skills but has to be rebuilt or invalidated whenever an import changes skills, and
  holds C·s entries in memory per server process. The SQL version is always consistent with
  the database, needs no warm-up, and stays under 200 ms at 100k. If ranking had to be much
  faster, I'd keep an in-memory index rebuilt after each import.
- **Score is a plain count.** Every required skill weighs the same. There's no weighting by
  rarity, seniority or recency, and no synonyms ("Postgres" ≠ "PostgreSQL"). Weighting would
  only change the scoring step; the index lookup stays the same.
- **Exact skill names only.** Matching ignores case and spacing but is otherwise exact.
- **Unexplained:** reusing one long-lived connection for repeated ranking queries measured
  ~25% slower than a fresh connection each time (246 ms against 190 ms for 3 common skills).
  Garbage collection, schema setup and threads ruled out; cause not determined. The API
  opens a connection per request, so it isn't affected.
