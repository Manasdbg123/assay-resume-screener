"""
Screening orchestrator.

Chooses an engine, and degrades rather than fails: if Claude is unavailable the
baseline scorer still returns a result, flagged `degraded=True` so callers and
dashboards can tell a real judgement from a keyword fallback.
"""

import logging
import time

from .baseline import screen_baseline
from .config import Config
from .llm import LLMUnavailable
from .schemas import ScreeningResponse

logger = logging.getLogger(__name__)


class ScreeningError(RuntimeError):
    """Raised when no engine could produce a result."""


_PROVIDERS = frozenset({"gemini", "claude"})


def _engine_for(provider: str):
    """
    Resolve a provider name to its screening function.

    Imported lazily so a deployment using one provider does not need the other's
    SDK installed.
    """
    if provider == "gemini":
        from .gemini import screen as engine
    else:
        from .llm import screen as engine
    return engine


def screen_resume(
    resume_text: str,
    job_description: str,
    config: Config,
    filename: str | None = None,
    client=None,
) -> ScreeningResponse:
    """Screen a resume with the configured LLM, falling back to the baseline engine."""
    # Validated up front: a typo in LLM_PROVIDER must fail loudly rather than
    # silently serving keyword scores that look like AI judgements.
    if config.llm_provider not in _PROVIDERS:
        raise ScreeningError(
            f"Unknown LLM_PROVIDER {config.llm_provider!r} - expected "
            f"{' or '.join(sorted(_PROVIDERS))}"
        )

    if config.llm_available:
        try:
            engine_screen = _engine_for(config.llm_provider)
            result, meta = engine_screen(resume_text, job_description, config, client=client)
            return ScreeningResponse(
                result=result,
                engine=config.llm_provider,
                model=meta["model"],
                filename=filename,
                latency_ms=meta["latency_ms"],
                degraded=False,
            )
        except LLMUnavailable as exc:
            if not config.fallback_to_baseline:
                raise ScreeningError(str(exc)) from exc
            logger.warning(
                "llm engine unavailable, falling back to baseline",
                extra={"provider": config.llm_provider, "reason": str(exc)},
            )
    else:
        logger.info(
            "llm engine not configured, using baseline",
            extra={"provider": config.llm_provider},
        )

    started = time.perf_counter()
    try:
        result = screen_baseline(resume_text, job_description)
    except Exception as exc:  # the baseline is the last line of defence
        raise ScreeningError(f"Baseline scoring failed: {exc}") from exc

    return ScreeningResponse(
        result=result,
        engine="baseline",
        model=None,
        filename=filename,
        latency_ms=int((time.perf_counter() - started) * 1000),
        degraded=config.llm_enabled,
    )
