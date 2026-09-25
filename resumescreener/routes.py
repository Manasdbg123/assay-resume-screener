"""HTTP routes: the screening API plus Kubernetes health probes."""

import logging

from flask import Blueprint, current_app, g, jsonify, request

from .jobs import JobStore, match_jobs
from .jobs.lookup import CompanyLookupError, lookup_company
from .jobs.sources import ATS_FETCHERS
from .parsing import ParsingError, extract_text
from .screener import screen_resume

logger = logging.getLogger(__name__)

api = Blueprint("api", __name__)

MIN_JD_CHARS = 40
MIN_RESUME_CHARS = 50

MAX_PAGE_SIZE = 100
SNIPPET_CHARS = 260


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


def _job_summary(job) -> dict:
    """A posting for a result list: everything but the full text."""
    data = job.model_dump(mode="json", exclude={"description"})
    snippet = " ".join(job.description.split())
    data["snippet"] = snippet[:SNIPPET_CHARS] + ("…" if len(snippet) > SNIPPET_CHARS else "")
    return data


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


@api.get("/jobs")
def browse_jobs():
    """
    Browse every stored posting - no CV required.

    Query params: q, company, location, remote=true, source, days (posted within),
    sort (recent|company|title), page (1-based), per_page (max 100).
    """
    config = current_app.config["APP_CONFIG"]
    per_page = _int_arg("per_page", 24, 1, MAX_PAGE_SIZE)
    page = _int_arg("page", 1, 1, 10_000)
    days = _int_arg("days", 0, 0, 3650)

    jobs, total = _job_store(config).search(
        query=request.args.get("q", "").strip()[:200],
        company=request.args.get("company", "").strip()[:200],
        location=request.args.get("location", "").strip()[:200],
        remote_only=request.args.get("remote", "").lower() in {"1", "true", "yes"},
        source=request.args.get("source", "").strip()[:40],
        posted_within_days=days or None,
        sort=request.args.get("sort", "recent"),
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    return jsonify({
        "jobs": [_job_summary(j) for j in jobs],
        "total": total,
        "page": page,
        "per_page": per_page,
        "has_more": page * per_page < total,
    })


@api.get("/jobs/detail")
def job_detail():
    """One posting with its full description. `id` is the namespaced posting id."""
    config = current_app.config["APP_CONFIG"]
    job = _job_store(config).get(request.args.get("id", ""))
    if job is None:
        return jsonify({"error": "That posting is no longer in the index."}), 404
    return jsonify(job.model_dump(mode="json"))


@api.get("/jobs/companies")
def job_companies():
    """Companies in the index with their open-role counts."""
    config = current_app.config["APP_CONFIG"]
    return jsonify({
        "companies": _job_store(config).companies(
            query=request.args.get("q", "").strip()[:100],
            limit=_int_arg("limit", 60, 1, 500),
        ),
        "boards": sorted(ATS_FETCHERS),
    })


@api.post("/jobs/lookup")
def company_lookup():
    """
    Fetch every current opening at one company, from whichever ATS hosts it.

    JSON body: {"company": "<name or careers URL>", "refresh": false}
    """
    config = current_app.config["APP_CONFIG"]
    body = request.get_json(silent=True) or {}
    try:
        result = lookup_company(
            str(body.get("company", "")),
            _job_store(config),
            cache_hours=config.job_lookup_cache_hours,
            refresh=bool(body.get("refresh")),
        )
    except CompanyLookupError as exc:
        return jsonify({"error": str(exc)}), 400

    logger.info(
        "company lookup completed",
        extra={
            "trace.id": getattr(g, "request_id", None),
            "company": result.company,
            "boards": len(result.boards),
            "postings": len(result.postings),
            "cached": result.cached,
        },
    )
    return jsonify({
        "company": result.company,
        "found": result.found,
        "cached": result.cached,
        "boards": [b.as_dict() for b in result.boards],
        "total": len(result.postings),
        "jobs": [_job_summary(j) for j in result.postings],
    })


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
