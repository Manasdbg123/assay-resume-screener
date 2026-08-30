"""
Pydantic schemas for screening results.

These double as the JSON Schema handed to Claude via structured outputs, so the
model is constrained to emit exactly this shape - no post-hoc JSON repair.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Verdict(str, Enum):  # noqa: UP042 - StrEnum needs 3.11+ and this stays 3.10-compatible
    STRONG_MATCH = "strong_match"
    MATCH = "match"
    PARTIAL_MATCH = "partial_match"
    WEAK_MATCH = "weak_match"


class SkillEvidence(BaseModel):
    """A single required skill, judged against the resume with its evidence."""

    skill: str = Field(description="The skill as named in the job description.")
    present: bool = Field(description="Whether the resume demonstrates this skill.")
    evidence: str = Field(
        description=(
            "Short quote or paraphrase from the resume supporting the judgement. "
            "Empty string when the skill is absent."
        )
    )
    strength: Literal["none", "mentioned", "applied", "expert"] = Field(
        description=(
            "none: absent. mentioned: listed only. applied: used in real work. "
            "expert: led or specialised in it."
        )
    )


class DimensionScore(BaseModel):
    score: float = Field(ge=0, le=100, description="Score from 0-100.")
    reasoning: str = Field(description="One or two sentences justifying the score.")


class Suggestion(BaseModel):
    priority: Literal["high", "medium", "low"]
    category: Literal["skills", "experience", "education", "presentation", "keywords"]
    message: str = Field(description="Specific, actionable advice for the candidate.")


class ScreeningResult(BaseModel):
    """The complete structured judgement Claude returns for one resume/JD pair."""

    overall_score: float = Field(ge=0, le=100)
    verdict: Verdict
    summary: str = Field(description="Two-sentence hiring summary of the candidate.")

    skills: DimensionScore
    experience: DimensionScore
    education: DimensionScore
    domain_relevance: DimensionScore

    skill_evidence: list[SkillEvidence] = Field(
        description="One entry per skill the job description requires."
    )
    matched_skills: list[str]
    missing_skills: list[str]
    transferable_skills: list[str] = Field(
        description="Resume skills that are adjacent to, but not exactly, JD requirements."
    )

    years_experience: float | None = Field(
        default=None, description="Total relevant years inferred from the resume."
    )
    required_years: float | None = Field(
        default=None, description="Years the job description asks for, if stated."
    )

    red_flags: list[str] = Field(
        description="Gaps, inconsistencies, or concerns worth a human look. May be empty."
    )
    suggestions: list[Suggestion]


class ScreeningResponse(BaseModel):
    """What the HTTP API returns - the result plus provenance metadata."""

    result: ScreeningResult
    engine: Literal["gemini", "claude", "baseline"]
    model: str | None = None
    filename: str | None = None
    latency_ms: int | None = None
    degraded: bool = Field(
        default=False,
        description="True when the LLM was unavailable and the baseline scorer ran instead.",
    )
