# Question 2a: Ranking at scale

> Each candidate has a set of skill tags. A client search has a required skill list. Write a
> function that ranks candidates by best skill-overlap match, efficient enough to run over
> 100,000 candidates.

## Approach: inverted index + bounded heap

Score each candidate by the number of distinct required skills they have. Ties are broken by
candidate id, so the order is deterministic. Candidates with no required skill aren't returned.

Instead of checking every candidate against the query, build an **inverted index** once: a map
from each skill to the ids of the candidates who have it. A query reads only the lists for the
skills it asks for, counts hits per candidate, and keeps the best `k` with a heap.

```python
import heapq
from collections import defaultdict


def normalize(skill):
    """Case- and spacing-insensitive: '  PostgreSQL ' and 'postgresql' match."""
    return " ".join(skill.split()).lower()


class SkillIndex:
    """Inverted index: skill -> ids of the candidates who have it.

    candidates: iterable of (candidate_id, skill_tags).
    Build once (after each import); then answer any number of queries.
    """

    def __init__(self, candidates):
        self.postings = defaultdict(list)
        for candidate_id, skills in candidates:
            for skill in {normalize(s) for s in skills}:   # a set: duplicate tags count once
                self.postings[skill].append(candidate_id)

    def rank(self, required_skills, k=20):
        """Return up to k (candidate_id, score) pairs, best first.

        score = how many distinct required skills the candidate has.
        Ties go to the lower candidate id, so results are deterministic.
        """
        scores = defaultdict(int)
        for skill in {normalize(s) for s in required_skills}:
            for candidate_id in self.postings.get(skill, ()):
                scores[candidate_id] += 1
        # Top k without sorting every match: a heap of size k.
        return heapq.nsmallest(k, scores.items(), key=lambda item: (-item[1], item[0]))
```

Usage:

```python
index = SkillIndex([
    (1, ["Python", "FastAPI", "Docker"]),
    (2, ["python", "Django"]),
    (3, ["Go", "Docker", "Kubernetes"]),
])
index.rank(["Python", "Docker", "Kubernetes"], k=2)
# [(1, 2), (3, 2)]   - both have 2 of 3; id 1 wins the tie
```

## Complexity

Notation: **C** = candidates, **s** = average skills per candidate, **q** = required skills in
the query, **P** = total length of the q posting lists (the candidate–skill pairs that match),
**M** = candidates with at least one match (M ≤ P, M ≤ C), **k** = results wanted.

| | Time | Space |
|---|---|---|
| Build the index (once) | O(C·s) | O(C·s) |
| Each query | **O(q + P + M log k)** | O(M) for the score map, O(k) for the heap |

- **Counting is O(P).** The query touches only candidates who have at least one requested
  skill. A rare skill on 1% of 100,000 candidates costs about 1,000 steps, not 100,000.
- **Selecting the top k is O(M log k)** with a heap of size k, against O(M log M) for sorting
  every match. With k = 20 and M in the tens of thousands, log k ≈ 4.3 and log M ≈ 16.
- **Worst case:** if every requested skill is on almost every candidate, P approaches q·C and
  the query does about as much work as a full scan. It's never asymptotically worse than a
  full scan, though, and real skill distributions are skewed, so most queries land far below it.

## Why not something simpler?

**The simple version** loops over every candidate, intersects their skills with the query, and
sorts the matches. Its cost is O(C·s + M log M) per query. That's correct, and it's the
reference implementation the faster versions are tested against (`rank_naive` in
[app/ranking.py](app/ranking.py)). But it reads all 100,000 candidates on every search, even
when only a few hundred could possibly match. The index costs O(C·s) memory once and makes
each query proportional to how many candidates actually match.

**Measured** on 100,000 synthetic candidates (750k candidate–skill pairs, skewed skill
popularity), median of 7 runs, top 20
([benchmarks/ranking_benchmark.py](benchmarks/ranking_benchmark.py)):

| Query | Matches (M) | Full scan | Inverted index |
|---|---:|---:|---:|
| 1 rare skill | 1,454 | 232 ms | **1.3 ms** |
| 1 common skill (on 54% of candidates) | 54,056 | 311 ms | **45 ms** |
| 3 common skills | 77,927 | 337 ms | **78 ms** |
| 10 mixed skills | 54,634 | 317 ms | **61 ms** |

The full scan costs about the same whatever you search for, because it always reads everyone.
The index is 4–180× faster, and the gap is largest for the specific searches clients actually run.

**Not chosen:**

- **Bitsets per skill** (AND/popcount) are fast for strict "has all skills" filters. But
  overlap ranking needs a count per candidate, not a yes/no answer, so you'd still pay for
  every candidate.
- **A search engine** (Elasticsearch, etc.) uses this same inverted index internally. At 100k
  candidates it adds infrastructure without changing the complexity.
- **Bucketing by score instead of a heap** (scores are integers from 1 to q) would make
  selection O(M). With k ≤ 200 the heap is already cheap, and it would still work if scores
  later became weighted (for example, rare skills counting more).

## How it's used in this project

The API's `/api/rank` runs the same algorithm in SQLite: the index `ix_cs_skill (skill_id,
candidate_id)` serves as the posting lists, followed by `GROUP BY` and `ORDER BY … LIMIT`
([app/repository.py](app/repository.py) `rank_candidates`). That keeps results consistent
with the database with no in-memory index to rebuild, at a worst case of 194 ms for a full API
call over 100k candidates. The in-memory `SkillIndex` is 2–4× faster on common skills, and
would be the next step if ranking had to get faster. The production version also breaks ties
by years of experience. Full details are in [docs/RANKING.md](docs/RANKING.md).
