"""Pydantic request and response models for the API."""

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, field_validator

TAG_MAX_LENGTH = 40
_TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9 _-]*")


def normalize_tag(value: str) -> str:
    """Collapse whitespace and lowercase; then allow only a-z, 0-9, space, '-' and '_'."""
    tag = " ".join(value.split()).lower()
    if len(tag) > TAG_MAX_LENGTH or not _TAG_PATTERN.fullmatch(tag):
        raise ValueError(
            f"tag must be 1-{TAG_MAX_LENGTH} characters: letters, digits, spaces, '-' or '_', "
            "starting with a letter or digit"
        )
    return tag


Tag = Annotated[str, AfterValidator(normalize_tag)]


class HealthOut(BaseModel):
    status: Literal["ok", "error"]
    db: Literal["ok", "error"]
    candidates: int | None  # None when the database could not be read
    ai_configured: bool
    demo_accounts: list[dict[str, str]] = []  # only when GCCX_DEMO_LOGIN is on


class SortOrder(StrEnum):
    """Allowed values for ?sort=. A leading '-' means descending."""

    APPLIED_DATE_DESC = "-applied_date"
    APPLIED_DATE = "applied_date"
    NAME = "name"
    NAME_DESC = "-name"
    YEARS_DESC = "-years_experience"
    YEARS = "years_experience"


class CandidateQuery(BaseModel):
    """Query parameters for GET /api/candidates. Unknown parameters are rejected."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(None, max_length=100, description="Words to find in name, role, notes or skills")
    role: str | None = Field(None, max_length=100, description="Exact target_role")
    source: str | None = Field(None, max_length=100, description="Exact source")
    tag: str | None = Field(None, max_length=40, description="Recruiter tag (case-insensitive)")
    shortlisted: bool | None = None
    sort: SortOrder = SortOrder.APPLIED_DATE_DESC
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0, le=1_000_000_000)

    @field_validator("q", "role", "source", "tag", mode="before")
    @classmethod
    def _blank_is_no_filter(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("tag")
    @classmethod
    def _tags_are_lowercase(cls, value):
        # Same folding as normalize_tag, but any text is allowed: an odd value just matches nothing.
        return " ".join(value.split()).lower() if value else value


class CandidateItem(BaseModel):
    id: str
    name: str
    target_role: str
    years_experience: int | None
    source: str
    applied_date: str | None
    skills: list[str]
    tags: list[str]
    shortlisted: bool


class CandidateDetail(CandidateItem):
    notes: str


class CandidatePage(BaseModel):
    items: list[CandidateItem]
    total: int
    limit: int
    offset: int


class FacetValue(BaseModel):
    value: str
    count: int


RANK_MAX_SKILLS = 20
SKILL_MAX_LENGTH = 60


def clean_skill_list(values):
    """Collapse whitespace, drop blanks, drop case-insensitive duplicates (first spelling wins)."""
    cleaned, seen = [], set()
    for value in values:
        skill = " ".join(value.split())
        if len(skill) > SKILL_MAX_LENGTH:
            raise ValueError(f"each skill must be at most {SKILL_MAX_LENGTH} characters")
        if skill and skill.casefold() not in seen:
            seen.add(skill.casefold())
            cleaned.append(skill)
    return cleaned


class RankQuery(BaseModel):
    """Query parameters for GET /api/rank. Repeat `skills` once per skill."""

    model_config = ConfigDict(extra="forbid")

    skills: list[str] = Field(min_length=1, max_length=RANK_MAX_SKILLS)
    role: str | None = Field(None, max_length=100)
    source: str | None = Field(None, max_length=100)
    limit: int = Field(20, ge=1, le=200)
    offset: int = Field(0, ge=0, le=1_000_000_000)

    @field_validator("role", "source", mode="before")
    @classmethod
    def _blank_is_no_filter(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("skills")
    @classmethod
    def _clean_skills(cls, values):
        cleaned = clean_skill_list(values)
        if not cleaned:
            raise ValueError("at least one non-blank skill is required")
        return cleaned


class RankedCandidate(BaseModel):
    candidate: CandidateItem
    score: int  # number of required skills the candidate has
    coverage: float  # score / number of recognised required skills
    matched_skills: list[str]
    missing_skills: list[str]


class RankResponse(BaseModel):
    query_skills: list[str]  # recognised skills, in the dataset's spelling
    unknown_skills: list[str]  # requested skills no candidate has
    items: list[RankedCandidate]
    total: int  # candidates with at least one required skill (after filters)
    limit: int
    offset: int


class SummaryRequest(BaseModel):
    """Body for POST /api/candidates/{id}/summary. Both fields are optional."""

    model_config = ConfigDict(extra="forbid")

    requirements: list[str] = Field(default_factory=list, max_length=RANK_MAX_SKILLS)
    refresh: StrictBool = False  # true = ignore the cache and call the model again

    @field_validator("requirements")
    @classmethod
    def _clean_requirements(cls, values):
        return clean_skill_list(values)


class SummaryLookup(BaseModel):
    """Query for GET /api/candidates/{id}/summary (read the cache only): ?requirements=Python&requirements=Go"""

    model_config = ConfigDict(extra="forbid")

    requirements: list[str] = Field(default_factory=list, max_length=RANK_MAX_SKILLS)

    @field_validator("requirements")
    @classmethod
    def _clean_requirements(cls, values):
        return clean_skill_list(values)


class PageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0, le=1_000_000_000)


class AuditQuery(PageQuery):
    action: str | None = Field(None, max_length=64, pattern=r"^[a-z_]+$")


class EvidenceClaim(BaseModel):
    claim: str
    evidence: str  # verbatim text found in the candidate's record


class Contradiction(EvidenceClaim):
    source: Literal["data_check", "model"]  # data_check = found by code, not the model


class DroppedClaim(EvidenceClaim):
    section: str


class SkillGaps(BaseModel):
    required_missing: list[str]  # requested skills not in the structured skills list
    required_in_notes_only: list[str]  # ...of those, ones the notes do mention
    role_gaps: list[str]  # the model's judgement of skills the target role usually needs


class Grounding(BaseModel):
    claims_returned: int
    claims_dropped: int  # claims whose evidence was not found in the record
    dropped: list[DroppedClaim]


class SummaryOut(BaseModel):
    candidate_id: str
    summary: str
    fit: Literal["strong", "moderate", "weak", "insufficient_evidence"]
    strengths: list[EvidenceClaim]
    concerns: list[EvidenceClaim]
    contradictions: list[Contradiction]
    skill_gaps: SkillGaps
    missing_information: list[str]
    requirements: list[str]
    grounding: Grounding
    model: str
    prompt_version: str
    generated_at: str
    cached: bool


Role = Literal["admin", "hiring_manager", "recruiter"]


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)  # long inputs are refused before hashing


class UserOut(BaseModel):
    """Public account fields. The password hash and sessions are never returned."""

    id: int
    email: str
    role: Role
    is_active: bool
    created_at: str
    updated_at: str
    last_login_at: str | None


class MeOut(BaseModel):
    user: UserOut
    permissions: list[str]
    csrf_token: str  # send back as the X-CSRF-Token header on POST, PUT, PATCH and DELETE
    session_expires_at: str


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class UserCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254)
    role: Role
    password: str = Field(max_length=1024)


class UserUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    is_active: StrictBool | None = None


class PasswordResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(max_length=1024)


class AuditEntry(BaseModel):
    id: int
    created_at: str
    actor_email: str | None
    action: str
    target_type: str | None
    target_id: str | None
    candidate_name: str | None
    details: dict | None
    ip: str | None = None


class AuditPage(BaseModel):
    items: list[AuditEntry]
    total: int
    limit: int
    offset: int


class TagIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag: Tag


class CandidateTags(BaseModel):
    id: str
    tags: list[str]


class ShortlistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shortlisted: StrictBool  # JSON true/false only; "yes" or 1 are rejected


class ShortlistOut(BaseModel):
    id: str
    shortlisted: bool
    shortlisted_at: str | None


class Facets(BaseModel):
    total: int
    shortlisted: int
    roles: list[FacetValue]
    sources: list[FacetValue]
    skills: list[FacetValue]
    tags: list[FacetValue]
