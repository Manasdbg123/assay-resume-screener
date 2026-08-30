"""Schemas for job postings and match results."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..schemas import ScreeningResult

# How much of a posting is embedded. See JobPosting.embedding_text.
EMBED_TEXT_CHARS = 2000


class JobPosting(BaseModel):
    """
    A normalised job posting.

    Every source (Greenhouse, Lever, Remotive, ...) is mapped into this shape, so
    the ranking and scoring stages never learn which board a job came from.
    """

    id: str = Field(description="Stable id, namespaced by source: 'greenhouse:12345'.")
    source: str
    title: str
    company: str
    location: str = ""
    url: str = ""
    description: str = Field(description="Full job text - what the scorer reasons over.")
    posted_at: datetime | None = None
    remote: bool = False
    tags: list[str] = Field(default_factory=list)

    @property
    def searchable_text(self) -> str:
        """Full text for keyword and skill matching, which benefits from everything."""
        return f"{self.title}\n{self.company}\n{self.location}\n\n{self.description}"

    @property
    def embedding_text(self) -> str:
        """
        The slice that gets embedded - deliberately shorter than the full posting.

        Postings lead with the title, summary and requirements, then run several
        thousand characters of benefits, culture and EEO boilerplate. That tail is
        near-identical across every posting, so embedding it pulls every vector
        toward the same direction and washes out the distinctions retrieval
        depends on. Truncating to the signal-bearing head improves separation and
        cuts embedding tokens roughly six-fold.
        """
        head = self.description[:EMBED_TEXT_CHARS]
        return f"{self.title}\n{self.company}\n{self.location}\n\n{head}"


class JobMatch(BaseModel):
    """One recommended job, with the evidence behind the recommendation."""

    job: JobPosting
    match_score: float = Field(ge=0, le=100)
    # How this score was produced. The UI shows it, because a cheaply-ranked
    # result and a fully-reasoned one should never look identical to a user.
    scored_by: Literal["llm", "baseline", "retrieval"]
    retrieval_score: float = Field(
        description="Hybrid retrieval score (0-1) from the ranking stage."
    )
    result: ScreeningResult | None = Field(
        default=None,
        description="Full evidence-backed screening. Present only for LLM-scored jobs.",
    )


class JobMatchResponse(BaseModel):
    """The payload returned by POST /match-jobs."""

    matches: list[JobMatch]
    filename: str | None = None
    total_candidates_considered: int = Field(
        description="How many postings the funnel started from."
    )
    llm_scored_count: int
    ranker: str = Field(description="Which ranking strategy ran: 'hybrid' or 'tfidf'.")
    engine: str = Field(description="LLM provider used, or 'baseline' if degraded.")
    degraded: bool = False
    latency_ms: int | None = None
