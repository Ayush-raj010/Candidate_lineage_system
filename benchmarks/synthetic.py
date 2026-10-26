"""Deterministic synthetic candidates for ranking tests and benchmarks.

Records have the same shape as candidates.json. Skill popularity is skewed
(a few skills are very common, most are rare), which is closer to real data
than a uniform draw and makes popular-skill queries the expensive case.
"""

import itertools
import random
from datetime import date, timedelta

ROLES = [
    "Backend Engineer", "Frontend Engineer", "Full-Stack Engineer", "ML Engineer", "Data Engineer",
    "DevOps Engineer", "Platform Engineer", "Mobile Engineer (Android)", "Mobile Engineer (iOS)", "QA / SDET",
]
SOURCES = [
    "AngelList", "Conference", "Naukri", "University Program", "Referral", "Cold Outreach",
    "GCCX Sourced", "LinkedIn Inbound",
]
REAL_SKILLS = [
    "Next.js", "Terraform", "Docker", "REST APIs", "Selenium", "Spring Boot", "Kubernetes", "Swift",
    "System Design", "Kotlin", "Redis", "PostgreSQL", "AWS", "Go", "Vector DBs", "React", "Java", "Python",
    "Django", "TypeScript", "MongoDB", "LangChain", "LLM APIs", "Node.js", "FastAPI", "Kafka", "GCP",
    "GraphQL", "CI/CD", "Cypress",
]


def skill_pool(size=200):
    return REAL_SKILLS + [f"Skill {n:03d}" for n in range(len(REAL_SKILLS), size)]


def generate(count, *, seed=42, pool_size=200, skills_per_candidate=(3, 12)):
    rng = random.Random(seed)
    pool = skill_pool(pool_size)
    cum_weights = list(itertools.accumulate(1 / (rank + 1) ** 0.8 for rank in range(len(pool))))
    start = date(2026, 1, 1)
    records = []
    for i in range(count):
        wanted = rng.randint(*skills_per_candidate)
        skills = set()
        while len(skills) < wanted:
            skills.add(rng.choices(pool, cum_weights=cum_weights)[0])
        records.append({
            "id": f"syn_{i:06d}",
            "name": f"Synthetic Candidate {i}",
            "target_role": rng.choice(ROLES),
            "years_experience": None if rng.random() < 0.05 else rng.randint(0, 15),
            "source": rng.choice(SOURCES),
            "skills": sorted(skills),
            "notes": "",
            "applied_date": (start + timedelta(days=rng.randrange(270))).isoformat(),
        })
    return records
