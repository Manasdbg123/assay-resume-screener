"""
Tests for the baseline engine, focused on the term-boundary behaviour that the
naive whitespace matcher got wrong.
"""

import pytest

from resumescreener.baseline import extract_experience, extract_skills, screen_baseline


@pytest.mark.parametrize(
    "text,expected,unexpected",
    [
        # Trailing punctuation must not defeat a match.
        ("I use PostgreSQL.", "postgresql", None),
        ("Skills: Python, Django, AWS.", "django", None),
        ("(Kubernetes)", "kubernetes", None),
        # Substrings must not produce false positives.
        ("I write golang services", "golang", "go"),
        ("Built in C++", "c++", "c"),
        ("node.js backend", "node.js", None),
        ("react-native apps", "react native", None),
    ],
)
def test_term_boundaries(text, expected, unexpected):
    found = extract_skills(text)["found_skills"]
    assert expected in found
    if unexpected:
        assert unexpected not in found


def test_extracts_years_of_experience():
    assert extract_experience("7+ years of experience in backend")["max_years"] == 7


def test_implausible_year_counts_are_ignored():
    # A "1995-2024" date range must not be read as 1995 years of experience.
    assert extract_experience("Worked 1995 years")["max_years"] == 0


def test_strong_resume_outscores_weak_one(resume_text, job_description):
    strong = screen_baseline(resume_text, job_description)
    weak = screen_baseline(
        "Marcus Webb. Retail store manager. Managed inventory and staff rotas.",
        job_description,
    )
    assert strong.overall_score > weak.overall_score


def test_result_conforms_to_shared_schema(resume_text, job_description):
    result = screen_baseline(resume_text, job_description)
    assert 0 <= result.overall_score <= 100
    assert result.verdict.value in {"strong_match", "match", "partial_match", "weak_match"}
    assert result.suggestions
    # Every JD skill is accounted for as either matched or missing.
    accounted = set(result.matched_skills) | set(result.missing_skills)
    assert {e.skill for e in result.skill_evidence} == accounted
