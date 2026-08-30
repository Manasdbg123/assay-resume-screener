"""
The matching funnel: CV in, ranked jobs out.

    stage 1  retrieve   store query, filters          thousands -> ~2,000   free
    stage 2  rank       hybrid embedding + skills     ~2,000    -> ~30      1 API call
    stage 3  judge      the existing LLM screener     top ~10   -> scored   ~10 calls
    stage 4  tail       the existing baseline engine  the rest  -> scored   free

Why a funnel rather than scoring everything: an LLM screening takes ~14 seconds
and one unit of a small daily quota. Scoring 30 jobs directly would take seven
minutes and exhaust a free-tier day on a single upload. The funnel spends the
expensive engine only where the cheap stages already agree the job is promising.

Stage 3 runs its calls concurrently - they are independent network waits, so
ten sequential 14-second calls become roughly one.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from ..baseline import screen_baseline
from ..config import Config
from ..llm import LLMUnavailable
from ..screener import _engine_for
from .embeddings import embed_query
from .ranking import rank_hybrid, rank_tfidf
from .schemas import JobMatch, JobMatchResponse, JobPosting
from .store import JobStore

logger = logging.getLogger(__name__)


def _llm_score_one(
    posting: JobPosting,
    resume_text: str,
    config: Config,
    retrieval_score: float,
    client=None,
) -> JobMatch:
    """Score one posting with the LLM, degrading to the baseline on failure."""
    try:
        engine = _engine_for(config.llm_provider)
        result, _ = engine(resume_text, posting.description, config, client=client)
        return JobMatch(
            job=posting, match_score=result.overall_score, scored_by="llm",
            retrieval_score=retrieval_score, result=result,
        )
    except LLMUnavailable as exc:
        # One job failing must not sink the whole result set - the user still
        # gets a ranked list, with this entry honestly labelled as cheaper.
        logger.warning(
            "llm scoring failed for job, using baseline",
            extra={"job_id": posting.id, "reason": str(exc)[:200]},
        )
        result = screen_baseline(resume_text, posting.description)
        return JobMatch(
            job=posting, match_score=result.overall_score, scored_by="baseline",
            retrieval_score=retrieval_score, result=result,
        )


def match_jobs(
    resume_text: str,
    config: Config,
    store: JobStore,
    filename: str | None = None,
    remote_only: bool = False,
    sources: list[str] | None = None,
    client=None,
) -> JobMatchResponse:
    """
    Find the jobs this CV matches best.

    Raises:
        LLMUnavailable: only when the CV itself cannot be embedded, which means
            no ranking is possible at all. Every later stage degrades instead.
    """
    started = time.perf_counter()

    # --- stage 1: retrieve ---
    postings, matrix = store.load_for_matching(
        remote_only=remote_only, sources=sources, limit=config.job_retrieval_limit
    )
    if not postings:
        return JobMatchResponse(
            matches=[], filename=filename, total_candidates_considered=0,
            llm_scored_count=0, ranker="none", engine=config.llm_provider,
            degraded=False, latency_ms=0,
        )

    # --- stage 2: rank ---
    degraded = False
    try:
        query_vector = embed_query(resume_text, config, client=None)
        ranked = rank_hybrid(
            resume_text, query_vector, postings, matrix, config.job_shortlist_size
        )
        ranker = "hybrid"
    except LLMUnavailable as exc:
        # Without embeddings the feature still works, just less well - TF-IDF is
        # a genuine fallback here, not a placeholder.
        logger.warning(
            "embedding unavailable, ranking with tfidf",
            extra={"reason": str(exc)[:200]},
        )
        ranked = rank_tfidf(resume_text, postings, config.job_shortlist_size)
        ranker = "tfidf"
        degraded = True

    shortlist = [(postings[i], score) for i, score in ranked]

    # --- stage 3: judge the top slice with the LLM, concurrently ---
    llm_slice = shortlist[:config.job_llm_scored_count] if config.llm_available else []
    tail = shortlist[len(llm_slice):]

    matches: list[JobMatch] = []
    if llm_slice:
        with ThreadPoolExecutor(max_workers=config.job_llm_concurrency) as pool:
            futures = [
                pool.submit(_llm_score_one, posting, resume_text, config, score, client)
                for posting, score in llm_slice
            ]
            matches = [f.result() for f in futures]

    # --- stage 4: score the tail with the free engine ---
    for posting, score in tail:
        result = screen_baseline(resume_text, posting.description)
        matches.append(JobMatch(
            job=posting, match_score=result.overall_score, scored_by="baseline",
            retrieval_score=score, result=result,
        ))

    # LLM-scored jobs sort above baseline-scored ones at equal score: a reasoned
    # judgement is worth more than a keyword tally, and mixing them by raw score
    # would silently promote the cheaply-scored tail.
    matches.sort(key=lambda m: (m.scored_by == "llm", m.match_score), reverse=True)

    llm_count = sum(1 for m in matches if m.scored_by == "llm")
    if llm_slice and llm_count == 0:
        degraded = True

    return JobMatchResponse(
        matches=matches,
        filename=filename,
        total_candidates_considered=len(postings),
        llm_scored_count=llm_count,
        ranker=ranker,
        engine=config.llm_provider if llm_count else "baseline",
        degraded=degraded,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
