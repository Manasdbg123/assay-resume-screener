"""HTTP routes: the screening API plus Kubernetes health probes."""

import logging

from flask import Blueprint, current_app, g, jsonify, request

from .jobs import JobStore, match_jobs
from .parsing import ParsingError, extract_text
from .screener import screen_resume

logger = logging.getLogger(__name__)

api = Blueprint("api", __name__)

MIN_JD_CHARS = 40
MIN_RESUME_CHARS = 50


@api.post("/analyze")
def analyze():
    """
    Screen an uploaded resume against a job description.

    Form fields:
        resume: file upload (PDF, DOCX, or TXT)
        job_description: the full job description text
    """
    config = current_app.config["APP_CONFIG"]

    if "resume" not in request.files or not request.files["resume"].filename:
        return jsonify({"error": "No resume file uploaded."}), 400

    resume_file = request.files["resume"]
    job_description = request.form.get("job_description", "").strip()

    if len(job_description) < MIN_JD_CHARS:
        return jsonify({
            "error": f"Job description is too short - please paste at least "
                     f"{MIN_JD_CHARS} characters."
        }), 400

    resume_text = extract_text(resume_file)  # raises ParsingError, handled app-wide
    if len(resume_text.strip()) < MIN_RESUME_CHARS:
        raise ParsingError(
            "Could not read enough text from this resume. If it is a scanned or "
            "image-based PDF, please upload a text-based version."
        )

    logger.info(
        "screening started",
        extra={
            "trace.id": getattr(g, "request_id", None),
            "resume_chars": len(resume_text),
            "jd_chars": len(job_description),
        },
    )

    response = screen_resume(
        resume_text=resume_text,
        job_description=job_description,
        config=config,
        filename=resume_file.filename,
    )

    logger.info(
        "screening completed",
        extra={
            "trace.id": getattr(g, "request_id", None),
            "engine": response.engine,
            "degraded": response.degraded,
            "overall_score": response.result.overall_score,
            "verdict": response.result.verdict.value,
            "event.duration_ms": response.latency_ms,
        },
    )

    return jsonify(response.model_dump(mode="json"))


def _job_store(config) -> JobStore:
    """
    One store instance per app, created lazily.

    SQLite connections are opened per query inside JobStore, so sharing the
    object across threads is safe - it holds a path, not a connection.
    """
    if not hasattr(current_app, "_job_store"):
        current_app._job_store = JobStore(config.job_db_path)
    return current_app._job_store


@api.post("/match-jobs")
def match():
    """
    Find the jobs a CV matches best.

    Form fields:
        resume: file upload (PDF, DOCX, or TXT)
        remote_only: "true" to restrict to remote postings

    This is additive - /analyze (CV against one pasted job description) is
    unchanged and remains the primary flow.
    """
    config = current_app.config["APP_CONFIG"]

    if "resume" not in request.files or not request.files["resume"].filename:
        return jsonify({"error": "No resume file uploaded."}), 400

    resume_file = request.files["resume"]
    resume_text = extract_text(resume_file)   # ParsingError handled app-wide
    if len(resume_text.strip()) < MIN_RESUME_CHARS:
        raise ParsingError(
            "Could not read enough text from this resume. If it is a scanned or "
            "image-based PDF, please upload a text-based version."
        )

    store = _job_store(config)
    if store.count(embedded_only=True) == 0:
        return jsonify({
            "error": "No job postings are indexed yet. Run "
                     "'python -m resumescreener.jobs.ingest' to fetch and embed jobs."
        }), 503

    remote_only = request.form.get("remote_only", "").strip().lower() in {"1", "true", "yes"}

    logger.info(
        "job matching started",
        extra={
            "trace.id": getattr(g, "request_id", None),
            "resume_chars": len(resume_text),
            "remote_only": remote_only,
        },
    )

    response = match_jobs(
        resume_text=resume_text,
        config=config,
        store=store,
        filename=resume_file.filename,
        remote_only=remote_only,
    )

    logger.info(
        "job matching completed",
        extra={
            "trace.id": getattr(g, "request_id", None),
            "ranker": response.ranker,
            "engine": response.engine,
            "degraded": response.degraded,
            "candidates_considered": response.total_candidates_considered,
            "matches_returned": len(response.matches),
            "llm_scored": response.llm_scored_count,
            "top_score": response.matches[0].match_score if response.matches else None,
            "event.duration_ms": response.latency_ms,
        },
    )

    return jsonify(response.model_dump(mode="json"))


@api.get("/jobs/stats")
def job_stats():
    """What is in the job index - useful for checking whether ingest has run."""
    config = current_app.config["APP_CONFIG"]
    return jsonify(_job_store(config).stats())


@api.get("/healthz")
def healthz():
    """Liveness: the process is up. Must not depend on anything external."""
    return jsonify({"status": "ok"})


@api.get("/readyz")
def readyz():
    """
    Readiness: the app can serve traffic.

    A missing API key is reported but is NOT a failure - the baseline engine can
    still serve requests, so pulling the pod from the load balancer would make an
    outage worse rather than better.
    """
    config = current_app.config["APP_CONFIG"]
    return jsonify({
        "status": "ready",
        "engine": config.llm_provider if config.llm_available else "baseline",
        "model": config.active_model if config.llm_available else None,
        "degraded": config.llm_enabled and not config.llm_available,
    })
