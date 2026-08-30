"""
Tests for the Claude engine and the degradation path.

These use a stub client - the suite never makes a network call, so it runs in CI
without an API key and costs nothing.
"""

from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from resumescreener import llm
from resumescreener.config import Config
from resumescreener.schemas import (
    DimensionScore,
    ScreeningResult,
    SkillEvidence,
    Suggestion,
    Verdict,
)
from resumescreener.screener import ScreeningError, screen_resume


def _sample_result() -> ScreeningResult:
    return ScreeningResult(
        overall_score=82.0,
        verdict=Verdict.STRONG_MATCH,
        summary="Strong backend candidate with directly relevant platform experience.",
        skills=DimensionScore(score=85, reasoning="Owns the exact stack."),
        experience=DimensionScore(score=88, reasoning="Nine years, staff level."),
        education=DimensionScore(score=75, reasoning="BSc CS as required."),
        domain_relevance=DimensionScore(score=80, reasoning="Same problem domain."),
        skill_evidence=[
            SkillEvidence(skill="Kubernetes", present=True,
                          evidence="Led migration to Kubernetes", strength="expert"),
        ],
        matched_skills=["Kubernetes", "Python"],
        missing_skills=["FastAPI"],
        transferable_skills=["Django implies FastAPI familiarity"],
        years_experience=9.0,
        required_years=5.0,
        red_flags=[],
        suggestions=[Suggestion(priority="low", category="presentation",
                                message="Quantify the sharding project's scale.")],
    )


class StubMessages:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


class StubClient:
    def __init__(self, response=None, error=None):
        self.messages = StubMessages(response, error)


def _stub_response(result=None, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        parsed_output=result if result is not None else _sample_result(),
        stop_reason=stop_reason,
        stop_details=stop_details,
        model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=1200, output_tokens=800,
            cache_read_input_tokens=950, cache_creation_input_tokens=0,
        ),
        _request_id="req_test123",
    )


@pytest.fixture
def llm_config():
    return Config(
        llm_provider="claude",
        anthropic_api_key="sk-ant-test",
        llm_enabled=True,
        json_logs=False,
    )


def test_returns_parsed_result(llm_config):
    client = StubClient(_stub_response())
    result, meta = llm.screen("resume text", "job description", llm_config, client=client)
    assert result.overall_score == 82.0
    assert meta["model"] == "claude-opus-5"
    assert meta["cache_read_tokens"] == 950


def test_job_description_carries_the_cache_breakpoint(llm_config):
    """
    The JD is stable across every resume screened for one role, so it must be the
    cached prefix. If this ordering regresses, batch screening silently costs
    full price on every resume.
    """
    client = StubClient(_stub_response())
    llm.screen("resume text", "job description", llm_config, client=client)

    content = client.messages.calls[0]["messages"][0]["content"]
    assert "job_description" in content[0]["text"]
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    # The resume varies per request and must never be cached.
    assert "resume" in content[1]["text"]
    assert "cache_control" not in content[1]
    # The rubric is cached too.
    assert client.messages.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_long_inputs_are_truncated_not_rejected(llm_config):
    llm_config.max_resume_chars = 100
    client = StubClient(_stub_response())
    llm.screen("x" * 5000, "job description", llm_config, client=client)
    sent = client.messages.calls[0]["messages"][0]["content"][1]["text"]
    assert sent.count("x") == 100


def test_refusal_raises_rather_than_scoring_zero(llm_config):
    client = StubClient(_stub_response(
        stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"),
    ))
    with pytest.raises(llm.LLMUnavailable, match="declined"):
        llm.screen("resume", "jd", llm_config, client=client)


@pytest.mark.parametrize("error", [
    anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")),
    anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=httpx.Request("POST", "https://x")),
        body=None,
    ),
])
def test_api_errors_become_llm_unavailable(llm_config, error):
    client = StubClient(error=error)
    with pytest.raises(llm.LLMUnavailable):
        llm.screen("resume", "jd", llm_config, client=client)


def test_missing_api_key_raises(resume_text, job_description):
    config = Config(llm_provider="claude", anthropic_api_key="", llm_enabled=True)
    with pytest.raises(llm.LLMUnavailable, match="not set"):
        llm.screen(resume_text, job_description, config)


# --- degradation behaviour -------------------------------------------------

def test_falls_back_to_baseline_when_claude_is_down(llm_config, resume_text, job_description):
    client = StubClient(error=anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com")))
    response = screen_resume(resume_text, job_description, llm_config, client=client)

    assert response.engine == "baseline"
    assert response.degraded is True          # callers can tell this apart
    assert 0 <= response.result.overall_score <= 100


def test_fallback_can_be_disabled(llm_config, resume_text, job_description):
    llm_config.fallback_to_baseline = False
    client = StubClient(error=anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com")))
    with pytest.raises(ScreeningError):
        screen_resume(resume_text, job_description, llm_config, client=client)


def test_successful_claude_run_is_not_degraded(llm_config, resume_text, job_description):
    client = StubClient(_stub_response())
    response = screen_resume(resume_text, job_description, llm_config, client=client)
    assert response.engine == "claude"
    assert response.degraded is False
    assert response.model == "claude-opus-5"
