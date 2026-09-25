"""Tests for the in-process job refresh and keyword matching without embeddings."""

import pytest

from resumescreener.config import Config
from resumescreener.jobs import matcher, refresh
from resumescreener.jobs.schemas import JobPosting
from resumescreener.jobs.store import JobStore


@pytest.fixture
def cfg(tmp_path):
    return Config(llm_enabled=False, gemini_api_key="", job_db_path=str(tmp_path / "jobs.db"),
                  json_logs=False, log_level="WARNING", job_refresh_hours=6)


@pytest.fixture(autouse=True)
def _reset_refresh_state(monkeypatch):
    monkeypatch.setattr(refresh, "_started", False)
    monkeypatch.setattr(refresh, "_lock_handle", None)


def _job(i, description="Python and PostgreSQL services on Kubernetes"):
    return JobPosting(id=f"t:{i}", source="greenhouse", title=f"Engineer {i}",
                      company="Acme", description=description)


def test_refresh_once_fetches_and_records_completion(cfg, monkeypatch):
    store = JobStore(cfg.job_db_path)
    monkeypatch.setattr("resumescreener.jobs.ingest.fetch_and_store",
                        lambda s: (s.upsert_jobs([_job(1), _job(2)])))
    refresh.refresh_once(cfg, store)
    assert store.count() == 2
    status = refresh.refresh_status(cfg.job_db_path)
    assert status["refreshing"] is False and status["last_refresh"]


def test_status_is_visible_while_a_refresh_runs(cfg, monkeypatch):
    """Another worker process reads the same file, so it must say 'refreshing'."""
    seen = {}

    def fetch(store):
        seen.update(refresh.refresh_status(cfg.job_db_path))
        return 0, 0

    monkeypatch.setattr("resumescreener.jobs.ingest.fetch_and_store", fetch)
    refresh.refresh_once(cfg, JobStore(cfg.job_db_path))
    assert seen["refreshing"] is True


def test_a_failing_refresh_never_raises(cfg, monkeypatch):
    def boom(store):
        raise RuntimeError("network down")
    monkeypatch.setattr("resumescreener.jobs.ingest.fetch_and_store", boom)
    refresh.refresh_once(cfg, JobStore(cfg.job_db_path))
    assert refresh.refresh_status(cfg.job_db_path)["refreshing"] is False


def test_refresh_is_off_unless_configured(cfg):
    cfg.job_refresh_hours = 0
    assert refresh.start_background_refresh(cfg) is False


def test_only_one_process_becomes_the_refresher(cfg, monkeypatch):
    """The lock file is what stops every gunicorn worker hitting the job boards."""
    monkeypatch.setattr(refresh, "refresh_once", lambda c, s: None)
    assert refresh._take_refresher_lock(cfg.job_db_path) is True
    held = refresh._lock_handle
    monkeypatch.setattr(refresh, "_lock_handle", None)
    assert refresh._take_refresher_lock(cfg.job_db_path) is False   # a second worker
    held.close()


def test_matching_works_without_any_embeddings(cfg):
    """A deployment with no API key still matches, on keywords."""
    store = JobStore(cfg.job_db_path)
    store.upsert_jobs([_job(1), _job(2, "React TypeScript CSS dashboards")])
    result = matcher.match_jobs("Python PostgreSQL Kubernetes engineer", cfg, store)
    assert result.ranker == "tfidf"
    assert result.matches[0].job.id == "t:1"


def test_stats_report_refresh_state(client):
    body = client.get("/jobs/stats").get_json()
    assert "refreshing" in body and "last_refresh" in body
