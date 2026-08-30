"""Tests for the job store, focused on the caching behaviour that bounds cost."""

import numpy as np
import pytest

from resumescreener.jobs.schemas import JobPosting
from resumescreener.jobs.store import JobStore


def _posting(job_id="greenhouse:acme:1", description="Python and PostgreSQL", **kwargs):
    defaults = {
        "id": job_id, "source": "greenhouse", "title": "Backend Engineer",
        "company": "Acme", "description": description,
    }
    defaults.update(kwargs)
    return JobPosting(**defaults)


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs.db")


def test_creates_its_database_and_parent_directory(tmp_path):
    store = JobStore(tmp_path / "nested" / "deeper" / "jobs.db")
    assert store.count() == 0


def test_upsert_inserts_then_updates(store):
    assert store.upsert_jobs([_posting()]) == (1, 0)
    assert store.upsert_jobs([_posting(title="Senior Backend Engineer")]) == (0, 1)
    assert store.count() == 1


def test_unchanged_description_keeps_the_cached_embedding(store):
    """
    Re-ingesting an unchanged posting must not discard its vector - that would
    re-embed the entire corpus on every ingest run and burn the quota.
    """
    store.upsert_jobs([_posting()])
    store.save_embeddings({"greenhouse:acme:1": [0.1] * 8})
    assert store.count(embedded_only=True) == 1

    store.upsert_jobs([_posting(title="Backend Engineer II")])   # title changed only
    assert store.count(embedded_only=True) == 1


def test_changed_description_invalidates_the_embedding(store):
    """The cached vector describes text that no longer exists, so it must go."""
    store.upsert_jobs([_posting()])
    store.save_embeddings({"greenhouse:acme:1": [0.1] * 8})

    store.upsert_jobs([_posting(description="Now a Rust and Kafka role entirely")])
    assert store.count(embedded_only=True) == 0
    assert len(store.jobs_without_embeddings()) == 1


def test_load_for_matching_returns_an_aligned_matrix(store):
    store.upsert_jobs([_posting(f"id:{i}") for i in range(3)])
    store.save_embeddings({f"id:{i}": [float(i)] * 4 for i in range(3)})

    postings, matrix = store.load_for_matching()
    assert len(postings) == 3
    assert matrix.shape == (3, 4)
    # Row i of the matrix must be the vector for postings[i] - the ranker indexes
    # into both by position, so a misalignment would silently score wrong jobs.
    for index, posting in enumerate(postings):
        expected = float(posting.id.split(":")[1])
        assert np.allclose(matrix[index], expected)


def test_unembedded_jobs_are_excluded_from_matching(store):
    store.upsert_jobs([_posting("a"), _posting("b")])
    store.save_embeddings({"a": [1.0] * 4})

    postings, matrix = store.load_for_matching()
    assert [p.id for p in postings] == ["a"]
    assert matrix.shape == (1, 4)


def test_empty_store_returns_no_matrix(store):
    postings, matrix = store.load_for_matching()
    assert postings == []
    assert matrix is None


def test_remote_filter(store):
    store.upsert_jobs([_posting("onsite", remote=False), _posting("remote", remote=True)])
    store.save_embeddings({"onsite": [1.0] * 4, "remote": [2.0] * 4})

    postings, _ = store.load_for_matching(remote_only=True)
    assert [p.id for p in postings] == ["remote"]


def test_round_trips_every_field(store):
    original = _posting(
        location="Remote - EU", url="https://example.com/job",
        remote=True, tags=["python", "backend"],
    )
    store.upsert_jobs([original])
    restored = store.all_jobs()[0]

    assert restored.title == original.title
    assert restored.location == "Remote - EU"
    assert restored.url == "https://example.com/job"
    assert restored.remote is True
    assert restored.tags == ["python", "backend"]
