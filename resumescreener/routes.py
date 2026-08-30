"""HTTP routes: the screening API plus Kubernetes health probes."""

import logging

from flask import Blueprint, current_app, g, jsonify, request

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
