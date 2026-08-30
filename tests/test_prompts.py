"""
Guards on the scoring rubric.

The rubric is the product. These assertions exist because a live run regressed
on each of them at least once.
"""

import re

from resumescreener.prompts import SYSTEM_PROMPT


def test_forbids_gendered_language_about_the_candidate():
    """
    A live screening described a candidate named Priya as "she" - inferred from
    the name alone, which rule 5 forbids. Stating the no-inference rule was not
    enough; the prompt has to say what to write instead.
    """
    lowered = SYSTEM_PROMPT.lower()
    assert "gendered language" in lowered
    assert '"they"' in lowered or " they " in lowered
    assert "never as" in lowered


def test_forbids_inference_from_protected_proxies():
    lowered = SYSTEM_PROMPT.lower()
    for proxy in ("names", "schools", "locations"):
        assert proxy in lowered
    assert "protected attribute" in lowered


def test_penalises_keyword_stuffing():
    """The behaviour the whole adversarial half of the eval set tests for."""
    assert "keyword stuffing" in SYSTEM_PROMPT.lower()


def test_requires_evidence_for_every_requirement():
    assert "exactly one skill_evidence entry" in SYSTEM_PROMPT


def test_defines_every_verdict_band_in_the_schema():
    from resumescreener.schemas import Verdict

    for verdict in Verdict:
        assert verdict.value in SYSTEM_PROMPT


def test_rubric_has_no_unresolved_line_continuations():
    """A stray backslash-newline would ship literal '\' characters to the model."""
    assert "\\n" not in SYSTEM_PROMPT
    assert not re.search(r"\\s*$", SYSTEM_PROMPT)
