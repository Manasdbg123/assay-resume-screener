"""
Claude-powered screening engine.

Uses the Anthropic Messages API with structured outputs, so the model returns a
validated `ScreeningResult` rather than free text we have to parse.

Two deliberate design choices:

1. **Prompt caching.** The rubric (system prompt) is stable across every request
   and the job description is stable across every resume screened for one role.
   Both carry a `cache_control` breakpoint, so screening N resumes against one JD
   pays for the JD once instead of N times.
2. **Structured outputs.** `messages.parse()` validates the response against the
   Pydantic schema. A malformed response raises rather than silently scoring 0.
"""

import logging
import time

import anthropic

from .config import Config
from .prompts import SYSTEM_PROMPT
from .schemas import ScreeningResult

logger = logging.getLogger(__name__)

__all__ = ["LLMUnavailable", "SYSTEM_PROMPT", "screen"]


class LLMUnavailable(RuntimeError):
    """Raised when the model could not produce a result (network, quota, refusal)."""


# The rubric lives in prompts.py so every engine scores against identical
# instructions. Re-exported here for backwards compatibility.

def _build_client(config: Config) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=config.anthropic_api_key or None,
        timeout=float(config.request_timeout),
        max_retries=config.max_retries,
    )


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    logger.warning(
        "%s exceeded %d chars and was truncated", label, limit,
        extra={"limit": limit, "actual": len(text)},
    )
    return text[:limit]


def screen(
    resume_text: str,
    job_description: str,
    config: Config,
    client: anthropic.Anthropic | None = None,
) -> tuple[ScreeningResult, dict]:
    """
    Screen one resume against one job description.

    Returns:
        (result, metadata) where metadata carries model, latency and token usage.

    Raises:
        LLMUnavailable: the model could not be reached or refused the request.
    """
    if not config.anthropic_api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")

    client = client or _build_client(config)
    resume_text = _truncate(resume_text, config.max_resume_chars, "resume")
    job_description = _truncate(job_description, config.max_jd_chars, "job description")

    # Order matters for caching: the JD is stable across every resume screened for
    # this role, so it goes first and carries the breakpoint. The resume - the only
    # part that varies - comes after it and is never cached.
    content = [
        {
            "type": "text",
            "text": f"<job_description>\n{job_description}\n</job_description>",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                f"<resume>\n{resume_text}\n</resume>\n\n"
                "Screen this resume against the job description above."
            ),
        },
    ]

    started = time.perf_counter()
    try:
        response = client.messages.parse(
            model=config.model,
            max_tokens=config.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            thinking={"type": "adaptive"},
            output_config={"effort": config.effort},
            messages=[{"role": "user", "content": content}],
            output_format=ScreeningResult,
        )
    except anthropic.AuthenticationError as exc:
        raise LLMUnavailable("Anthropic API key is invalid or missing") from exc
    except anthropic.RateLimitError as exc:
        raise LLMUnavailable("Rate limited by the Anthropic API") from exc
    except anthropic.APIStatusError as exc:
        raise LLMUnavailable(f"Anthropic API error ({exc.status_code})") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMUnavailable("Could not reach the Anthropic API") from exc

    latency_ms = int((time.perf_counter() - started) * 1000)

    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        raise LLMUnavailable(f"Model declined the request (category={category})")

    result = response.parsed_output
    if result is None:
        raise LLMUnavailable("Model returned no parseable result")

    usage = response.usage
    metadata = {
        "model": response.model,
        "latency_ms": latency_ms,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "request_id": response._request_id,
    }
    logger.info("claude screening complete", extra=metadata)
    return result, metadata
