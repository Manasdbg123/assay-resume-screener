"""
Tests for the Gemini engine.

Like the Claude tests, these use a stub client - no network, no API key, no cost.
The retry tests patch `time.sleep` so backoff logic is exercised instantly.
"""

from types import SimpleNamespace

import pytest
from google.genai import errors

from resumescreener import gemini
from resumescreener.config import Config
from resumescreener.llm import LLMUnavailable
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
        overall_score=91.0,
        verdict=Verdict.STRONG_MATCH,
        summary="Deep settlement-systems experience matching the role directly.",
        skills=DimensionScore(score=90, reasoning="Owns the required stack."),
        experience=DimensionScore(score=95, reasoning="Nine years, principal level."),
        education=DimensionScore(score=80, reasoning="BEng as required."),
        domain_relevance=DimensionScore(score=94, reasoning="Payments domain."),
        skill_evidence=[
            SkillEvidence(skill="PostgreSQL", present=True,
                          evidence="rewrote the hot settlement query path", strength="expert"),
        ],
        matched_skills=["PostgreSQL", "Python"],
        missing_skills=["Django"],
        transferable_skills=["k8s is Kubernetes"],
        years_experience=9.0,
        required_years=5.0,
        red_flags=[],
        suggestions=[Suggestion(priority="low", category="presentation",
                                message="Name the technologies explicitly.")],
    )


def _api_error(status_code, message="boom"):
    """Build a google-genai error without going through the HTTP layer."""
    exc = errors.ClientError.__new__(
        errors.ClientError if status_code < 500 else errors.ServerError
    )
    Exception.__init__(exc, f"{status_code} {message}")
    exc.code = status_code
    exc.message = message
    return exc


class StubModels:
    def __init__(self, response=None, errors_before_success=()):
        self._response = response
        self._errors = list(errors_before_success)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._errors:
            raise self._errors.pop(0)
        return self._response


class StubClient:
    def __init__(self, response=None, errors_before_success=()):
        self.models = StubModels(response, errors_before_success)


_UNSET = object()


def _stub_response(result=_UNSET, cached=0):
    return SimpleNamespace(
        parsed=_sample_result() if result is _UNSET else result,
        usage_metadata=SimpleNamespace(
            prompt_token_count=830,
            candidates_token_count=910,
            cached_content_token_count=cached,
        ),
        response_id="resp_test",
        prompt_feedback=None,
    )


@pytest.fixture
def gemini_config():
    return Config(
        llm_provider="gemini",
        gemini_api_key="test-key",
        gemini_model="gemini-3.5-flash",
        llm_enabled=True,
        json_logs=False,
        max_retries=3,
    )


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda _: None)


def test_returns_parsed_result(gemini_config):
    client = StubClient(_stub_response())
    result, meta = gemini.screen("resume", "job description", gemini_config, client=client)
    assert result.overall_score == 91.0
    assert meta["model"] == "gemini-3.5-flash"
    assert meta["input_tokens"] == 830


def test_sends_the_shared_rubric_and_the_pydantic_schema(gemini_config):
    """
    Both engines must send the identical rubric, or the eval comparison measures
    prompt differences instead of model differences.
    """
    from resumescreener.prompts import SYSTEM_PROMPT

    client = StubClient(_stub_response())
    gemini.screen("resume text", "job description", gemini_config, client=client)

    call = client.models.calls[0]
    assert call["config"].system_instruction == SYSTEM_PROMPT
    assert call["config"].response_schema is ScreeningResult
    assert call["config"].response_mime_type == "application/json"
    assert "job_description" in call["contents"]
    assert "resume text" in call["contents"]


def test_long_inputs_are_truncated_not_rejected(gemini_config):
    gemini_config.max_resume_chars = 100
    client = StubClient(_stub_response())
    gemini.screen("x" * 5000, "job description", gemini_config, client=client)
    assert client.models.calls[0]["contents"].count("x") == 100


@pytest.mark.parametrize("status", [429, 503, 500])
def test_transient_failures_are_retried(gemini_config, status):
    """The free tier returns 429 and 503 routinely; one must not fail a screening."""
    client = StubClient(_stub_response(), errors_before_success=[_api_error(status)])
    result, _ = gemini.screen("resume", "jd", gemini_config, client=client)
    assert result.overall_score == 91.0
    assert len(client.models.calls) == 2


def test_retries_are_bounded(gemini_config):
    gemini_config.max_retries = 2
    client = StubClient(errors_before_success=[_api_error(503) for _ in range(5)])
    with pytest.raises(LLMUnavailable):
        gemini.screen("resume", "jd", gemini_config, client=client)
    assert len(client.models.calls) == 3   # initial attempt plus two retries


def test_permanent_client_errors_are_not_retried(gemini_config):
    """A bad key or malformed request will never succeed - retrying wastes time."""
    client = StubClient(errors_before_success=[_api_error(400, "invalid argument")])
    with pytest.raises(LLMUnavailable, match="rejected"):
        gemini.screen("resume", "jd", gemini_config, client=client)
    assert len(client.models.calls) == 1


def test_unparseable_response_raises_rather_than_scoring_zero(gemini_config):
    """A safety block returns None - that must not read as a candidate scoring 0."""
    client = StubClient(_stub_response(result=None))
    with pytest.raises(LLMUnavailable, match="no parseable result"):
        gemini.screen("resume", "jd", gemini_config, client=client)


def test_missing_api_key_raises():
    config = Config(llm_provider="gemini", gemini_api_key="", llm_enabled=True)
    with pytest.raises(LLMUnavailable, match="not set"):
        gemini.screen("resume", "jd", config)


# --- provider routing and degradation -------------------------------------

def test_screener_routes_to_gemini(gemini_config, resume_text, job_description):
    client = StubClient(_stub_response())
    response = screen_resume(resume_text, job_description, gemini_config, client=client)
    assert response.engine == "gemini"
    assert response.degraded is False
    assert response.model == "gemini-3.5-flash"


def test_falls_back_to_baseline_when_gemini_is_down(gemini_config, resume_text, job_description):
    client = StubClient(errors_before_success=[_api_error(503) for _ in range(9)])
    response = screen_resume(resume_text, job_description, gemini_config, client=client)
    assert response.engine == "baseline"
    assert response.degraded is True


def test_unknown_provider_is_rejected(resume_text, job_description):
    config = Config(llm_provider="chatgpt", gemini_api_key="x", llm_enabled=True)
    with pytest.raises(ScreeningError, match="Unknown LLM_PROVIDER"):
        screen_resume(resume_text, job_description, config)
