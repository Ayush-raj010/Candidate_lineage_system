"""Skill-overlap ranking, in plain Python.

The API ranks in SQL (repository.rank_candidates). This module holds the same
algorithm without a database, as the reference the SQL is tested against
and as the subject of the complexity write-up in docs/RANKING.md.

Score = number of distinct required skills the candidate has. Order: score
high to low, then years_experience high to low (unknown last), then id.
Candidates with no required skill are not ranked.

C = candidates, s = average skills per candidate, q = required skills,
P = total length of the q posting lists, M = candidates with score >= 1,
k = results wanted.
"""

import heapq
from collections import defaultdict

from app.importer import skill_key


def _order_key(candidate, score):
    years = candidate["years_experience"]
    return (-score, years is None, -(years or 0), candidate["id"])


def rank_naive(candidates, required_skills, k):
    """Baseline: score every candidate. O(C*s + C log C) time, O(C) space."""
    required = {skill_key(skill) for skill in required_skills}
    scored = []
    for candidate in candidates:
        score = len(required & {skill_key(skill) for skill in candidate["skills"]})
        if score:
            scored.append((_order_key(candidate, score), candidate["id"], score))
    scored.sort()
    return [(candidate_id, score) for _, candidate_id, score in scored[:k]]


class SkillIndex:
    """Inverted index: skill -> ids of candidates with that skill.

    Building it is O(C*s) time and space, once. Each query then reads only
    the posting lists of the required skills.
    """

    def __init__(self, candidates):
        self.candidates = {}
        self.postings = defaultdict(list)
        for candidate in candidates:
            self.candidates[candidate["id"]] = candidate
            for key in {skill_key(skill) for skill in candidate["skills"]}:
                self.postings[key].append(candidate["id"])

    def rank(self, required_skills, k):
        """O(P + M log k) time, O(M) space.

        Counting walks each required skill's posting list once (P). Picking
        the top k from the M matched candidates uses a bounded heap.
        """
        scores = defaultdict(int)
        for key in {skill_key(skill) for skill in required_skills}:
            for candidate_id in self.postings.get(key, ()):
                scores[candidate_id] += 1
        top = heapq.nsmallest(k, scores.items(), key=lambda item: _order_key(self.candidates[item[0]], item[1]))
        return [(candidate_id, score) for candidate_id, score in top]
