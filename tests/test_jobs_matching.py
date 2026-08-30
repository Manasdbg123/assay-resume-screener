"""
Tests for the matching funnel, its ranking, and the source parsers.

All network is stubbed - no job boards are fetched and no embeddings are bought.
"""

import numpy as np
import pytest

from resumescreener.config import Config
from resumescreener.jobs import matcher
from resumescreener.jobs.ranking import rank_hybrid, rank_tfidf, skill_overlap_scores
from resumescreener.jobs.schemas import EMBED_TEXT_CHARS, JobPosting
from resumescreener.jobs.sources import GreenhouseSource, _strip_html
from resumescreener.jobs.store import JobStore
from resumescreener.llm import LLMUnavailable

BACKEND = JobPosting(
    id="j:backend", source="test", title="Senior Backend Engineer", company="Acme",
    description=("Own the ledger service. 5+ years production Python, deep PostgreSQL "
                 "schema design and query optimisation, Kubernetes and Docker, AWS."),
)
FRONTEND = JobPosting(
    id="j:frontend", source="test", title="Frontend Engineer", company="Acme",
    description=("Build the dashboard. 3+ years React, TypeScript, strong CSS, "
                 "data visualisation, WCAG accessibility, Jest testing."),
)
DEVOPS = JobPosting(
    id="j:devops", source="test", title="Platform Engineer", company="Acme",
    description=("Own the Kubernetes platform. Terraform, ELK logging pipelines, "
                 "CI/CD, Linux, Go and Bash scripting."),
)
POSTINGS = [BACKEND, FRONTEND, DEVOPS]

BACKEND_CV = ("Staff engineer. Owned a ledger service. PostgreSQL sharding across 12 "
              "shards, ran services on Kubernetes and AWS. Python and Django, 9 years.")


# --- source parsing --------------------------------------------------------

def test_strip_html_keeps_requirement_bullets():
    """Requirement bullets are what the scorer reads; flattening them loses structure."""
    text = _strip_html("<p>About us</p><ul><li>5 years Python</li><li>AWS</li></ul>")
    assert "- 5 years Python" in text
    assert "- AWS" in text


def test_strip_html_drops_scripts_and_unescapes_entities():
    text = _strip_html("<script>alert(1)</script><p>R&amp;D team</p>")
    assert "alert" not in text
    assert "R&D team" in text


def test_greenhouse_parses_a_board_payload(monkeypatch):
    payload = {"jobs": [{
        "id": 42, "title": " Backend Engineer ", "absolute_url": "https://x/42",
        "updated_at": "2026-08-01T10:00:00Z", "location": {"name": "Remote - US"},
        "content": "<p>Build things with <b>Python</b></p>",
    }]}
    monkeypatch.setattr("resumescreener.jobs.sources._get", lambda *a, **k: payload)

    postings = GreenhouseSource(["acme"]).fetch()
    assert len(postings) == 1
    job = postings[0]
    assert job.id == "greenhouse:acme:42"
    assert job.title == "Backend Engineer"      # whitespace stripped
    assert job.remote is True                   # inferred from the location
    assert "Python" in job.description
    assert "<b>" not in job.description


def test_a_dead_board_does_not_abort_the_run(monkeypatch):
    """One 404 company must not lose the other 19 boards' postings."""
    def boom(*args, **kwargs):
        raise RuntimeError("404 Not Found")

    monkeypatch.setattr("resumescreener.jobs.sources._get", boom)
    assert GreenhouseSource(["dead-co"]).fetch() == []


def test_embedding_text_is_truncated_but_description_is_not():
    job = JobPosting(id="x", source="s", title="T", company="C", description="A" * 9000)
    assert len(job.description) == 9000            # the LLM still reads it all
    assert len(job.embedding_text) < EMBED_TEXT_CHARS + 200


# --- ranking ---------------------------------------------------------------

def test_skill_overlap_prefers_the_matching_role():
    scores = skill_overlap_scores(BACKEND_CV, POSTINGS)
    assert scores[0] > scores[1]      # backend beats frontend


def test_tfidf_ranker_returns_sorted_indices():
    ranked = rank_tfidf(BACKEND_CV, POSTINGS, top_k=3)
    assert len(ranked) == 3
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)


def test_hybrid_blends_semantics_with_skill_overlap():
    # Semantics deliberately point the wrong way: the frontend job is given the
    # vector closest to the query. Skill overlap should still pull backend up.
    query = np.array([1.0, 0.0], dtype=np.float32)
    matrix = np.array([[0.6, 0.8], [1.0, 0.0], [0.5, 0.85]], dtype=np.float32)

    ranked = rank_hybrid(BACKEND_CV, query, POSTINGS, matrix, top_k=3)
    order = [POSTINGS[i].id for i, _ in ranked]
    assert "j:backend" in order[:2], f"skill overlap ignored: {order}"


def test_hybrid_handles_identical_similarities():
    """Zero spread would divide by zero in the min-max normalisation."""
    query = np.array([1.0, 0.0], dtype=np.float32)
    matrix = np.array([[1.0, 0.0]] * 3, dtype=np.float32)
    ranked = rank_hybrid(BACKEND_CV, query, POSTINGS, matrix, top_k=3)
    assert len(ranked) == 3
    assert all(np.isfinite(score) for _, score in ranked)


# --- the funnel ------------------------------------------------------------

@pytest.fixture
def populated_store(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.upsert_jobs(POSTINGS)
    store.save_embeddings({
        "j:backend": [1.0, 0.0], "j:frontend": [0.0, 1.0], "j:devops": [0.7, 0.7],
    })
    return store


@pytest.fixture
def job_config():
    return Config(
        llm_provider="gemini", gemini_api_key="test-key", llm_enabled=True,
        json_logs=False, job_llm_scored_count=1, job_shortlist_size=3,
    )


@pytest.fixture
def _no_embedding_calls(monkeypatch):
    """The CV embedding is the one API call on the request path; stub it."""
    monkeypatch.setattr(
        matcher, "embed_query",
        lambda text, config, client=None: np.array([1.0, 0.0], dtype=np.float32),
    )


def _fake_llm(score):
    def _engine(resume, jd, config, client=None):
        from resumescreener.baseline import screen_baseline
        result = screen_baseline(resume, jd)
        result.overall_score = score
        return result, {"latency_ms": 10}
    return _engine


def test_returns_ranked_matches(populated_store, job_config, monkeypatch, _no_embedding_calls):
    monkeypatch.setattr(matcher, "_engine_for", lambda provider: _fake_llm(88.0))

    response = matcher.match_jobs(BACKEND_CV, job_config, populated_store)
    assert response.total_candidates_considered == 3
    assert len(response.matches) == 3
    assert response.ranker == "hybrid"
    assert response.llm_scored_count == 1


def test_only_the_top_slice_is_llm_scored(populated_store, job_config, monkeypatch,
                                          _no_embedding_calls):
    """
    The funnel exists to bound cost. If every shortlisted job were LLM-scored,
    one upload would exhaust a free-tier day.
    """
    calls = []

    def counting_engine(provider):
        def _engine(resume, jd, config, client=None):
            calls.append(jd)
            return _fake_llm(90.0)(resume, jd, config, client)
        return _engine

    monkeypatch.setattr(matcher, "_engine_for", counting_engine)
    matcher.match_jobs(BACKEND_CV, job_config, populated_store)

    assert len(calls) == job_config.job_llm_scored_count == 1


def test_llm_scored_jobs_outrank_baseline_ones(populated_store, job_config, monkeypatch,
                                               _no_embedding_calls):
    """
    A reasoned judgement outranks a keyword tally at equal score. Sorting purely
    by number would silently promote the cheaply-scored tail above it.
    """
    monkeypatch.setattr(matcher, "_engine_for", lambda provider: _fake_llm(10.0))

    response = matcher.match_jobs(BACKEND_CV, job_config, populated_store)
    assert response.matches[0].scored_by == "llm"
    assert response.matches[0].match_score == 10.0


def test_one_failing_job_does_not_sink_the_result_set(populated_store, job_config,
                                                      monkeypatch, _no_embedding_calls):
    def failing(provider):
        def _engine(resume, jd, config, client=None):
            raise LLMUnavailable("quota exhausted")
        return _engine

    monkeypatch.setattr(matcher, "_engine_for", failing)
    response = matcher.match_jobs(BACKEND_CV, job_config, populated_store)

    assert len(response.matches) == 3            # the user still gets a list
    assert all(m.scored_by == "baseline" for m in response.matches)
    assert response.degraded is True


def test_falls_back_to_tfidf_when_embeddings_are_unavailable(populated_store, job_config,
                                                             monkeypatch):
    def boom(text, config, client=None):
        raise LLMUnavailable("embedding quota exhausted")

    monkeypatch.setattr(matcher, "embed_query", boom)
    monkeypatch.setattr(matcher, "_engine_for", lambda provider: _fake_llm(70.0))

    response = matcher.match_jobs(BACKEND_CV, job_config, populated_store)
    assert response.ranker == "tfidf"
    assert response.degraded is True
    assert len(response.matches) == 3            # still usable


def test_empty_store_returns_no_matches_without_calling_anything(tmp_path, job_config):
    empty = JobStore(tmp_path / "empty.db")
    response = matcher.match_jobs(BACKEND_CV, job_config, empty)
    assert response.matches == []
    assert response.total_candidates_considered == 0


def test_baseline_only_config_skips_the_llm_entirely(populated_store, _no_embedding_calls):
    config = Config(llm_provider="gemini", gemini_api_key="", llm_enabled=False,
                    json_logs=False, job_shortlist_size=3)
    response = matcher.match_jobs(BACKEND_CV, config, populated_store)

    assert response.llm_scored_count == 0
    assert response.engine == "baseline"
    assert all(m.scored_by == "baseline" for m in response.matches)


def test_query_retries_are_far_shorter_than_ingest_retries():
    """
    A user waiting on a request must not sit through the ingest backoff.

    Measured before this split: a quota-exhausted CV embedding held an HTTP
    request open for 240 seconds before the TF-IDF fallback ran. Ingest is
    offline and can afford to outwait the burst window; a request cannot.
    """
    from resumescreener.jobs.embeddings import INGEST_RETRY_DELAYS, QUERY_RETRY_DELAYS

    assert sum(QUERY_RETRY_DELAYS) < 10
    assert sum(QUERY_RETRY_DELAYS) < sum(INGEST_RETRY_DELAYS) / 10
