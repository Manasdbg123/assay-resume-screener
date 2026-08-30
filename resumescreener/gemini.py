"""
Gemini-powered screening engine.

Mirrors the contract of `llm.py` (the Claude engine) exactly - same rubric from
`prompts.py`, same `ScreeningResult` schema, same `(result, metadata)` return and
same `LLMUnavailable` failure mode - so `screener.py` can swap providers with a
config change and `eval/` can compare them fairly.

Two differences from the Claude engine, both forced by the provider:

1. **No prompt caching.** Gemini's implicit caching is not controllable the way
   Anthropic's `cache_control` breakpoints are, so there is no JD-first ordering
   trick to exploit here. Screening N resumes for one role costs N full prompts.
2. **Explicit retries.** The free tier returns 429 (quota) and 503 (model busy)
   routinely, and the SDK does not retry them, so this module backs off itself.
"""

import logging
import random
import time

from google import genai
from google.genai import errors, types

from .config import Config
from .llm import LLMUnavailable
from .prompts import SYSTEM_PROMPT
from .schemas import ScreeningResult

logger = logging.getLogger(__name__)

# 429 is quota, 503 is a busy model, 500 is a transient server fault. All three
# are worth retrying; 400 (bad request) and 403 (bad key) never are.
_RETRYABLE_STATUS = {429, 500, 503}


def _build_client(config: Config) -> genai.Client:
    return genai.Client(api_key=config.gemini_api_key)


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    logger.warning(
        "%s exceeded %d chars and was truncated", label, limit,
        extra={"limit": limit, "actual": len(text)},
    )
    return text[:limit]


def _is_retryable(exc: errors.APIError) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code is None:
        # Some SDK versions only carry the status in the message.
        return any(str(s) in str(exc) for s in _RETRYABLE_STATUS)
    return code in _RETRYABLE_STATUS


def screen(
    resume_text: str,
    job_description: str,
    config: Config,
    client: genai.Client | None = None,
) -> tuple[ScreeningResult, dict]:
    """
    Screen one resume against one job description using Gemini.

    Returns:
        (result, metadata) with model, latency and token usage.

    Raises:
        LLMUnavailable: the model could not be reached, refused, or returned
            something that did not validate against the schema.
    """
    if not config.gemini_api_key:
        raise LLMUnavailable("GEMINI_API_KEY is not set")

    client = client or _build_client(config)
    resume_text = _truncate(resume_text, config.max_resume_chars, "resume")
    job_description = _truncate(job_description, config.max_jd_chars, "job description")

    prompt = (
        f"<job_description>\n{job_description}\n</job_description>\n\n"
        f"<resume>\n{resume_text}\n</resume>\n\n"
        "Screen this resume against the job description above."
    )

    generation_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        # Passing the Pydantic model constrains decoding to the schema, so the
        # response validates by construction rather than by hopeful parsing.
        response_schema=ScreeningResult,
        temperature=config.gemini_temperature,
        max_output_tokens=config.max_tokens,
        # This engine declares no tools, so the SDK's automatic function calling
        # is dead weight - and leaving it on emits a warning per request that
        # would otherwise be shipped to Elasticsearch on every screening.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    started = time.perf_counter()
    last_error: Exception | None = None

    for attempt in range(config.max_retries + 1):
        try:
            response = client.models.generate_content(
                model=config.gemini_model,
                contents=prompt,
                config=generation_config,
            )
            break
        except errors.ClientError as exc:
            last_error = exc
            if not _is_retryable(exc) or attempt == config.max_retries:
                raise LLMUnavailable(f"Gemini rejected the request: {exc}") from exc
        except errors.ServerError as exc:
            last_error = exc
            if attempt == config.max_retries:
                raise LLMUnavailable(f"Gemini is unavailable: {exc}") from exc
        except Exception as exc:  # network faults, DNS, timeouts
            raise LLMUnavailable(f"Could not reach Gemini: {exc}") from exc

        # Exponential backoff with jitter, so retries from concurrent workers do
        # not all land at the same instant and re-trigger the quota error.
        delay = min(2 ** attempt + random.uniform(0, 1), 30)
        logger.warning(
            "gemini call failed, retrying",
            extra={"attempt": attempt + 1, "delay_s": round(delay, 1),
                   "reason": str(last_error)[:200]},
        )
        time.sleep(delay)

    latency_ms = int((time.perf_counter() - started) * 1000)

    result = response.parsed
    if result is None:
        # Almost always a safety block or a truncated response that failed to
        # validate. Surface it rather than scoring the candidate zero.
        feedback = getattr(response, "prompt_feedback", None)
        raise LLMUnavailable(f"Gemini returned no parseable result (feedback={feedback})")

    usage = response.usage_metadata
    metadata = {
        "model": config.gemini_model,
        "latency_ms": latency_ms,
        "input_tokens": getattr(usage, "prompt_token_count", 0) or 0,
        "output_tokens": getattr(usage, "candidates_token_count", 0) or 0,
        # Gemini caches implicitly; the field is absent unless it applied.
        "cache_read_tokens": getattr(usage, "cached_content_token_count", 0) or 0,
        "cache_write_tokens": 0,
        "request_id": getattr(response, "response_id", None),
    }
    logger.info("gemini screening complete", extra=metadata)
    return result, metadata
