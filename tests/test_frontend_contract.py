"""
Contract tests between the API, the template, and the client script.

The v2 refactor changed the response shape, and a stale frontend fails silently
in the browser rather than loudly in CI. These tests close that gap.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "js" / "main.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def _js_element_ids():
    return set(re.findall(r"getElementById\('([^']+)'\)", JS))


def _html_element_ids():
    return set(re.findall(r'id="([^"]+)"', HTML))


def _js_created_ids():
    """Ids the script creates at runtime (e.g. the SVG gradient defs)."""
    return set(re.findall(r"\.id = '([^']+)'", JS))


def test_every_id_the_script_touches_exists_in_the_template():
    missing = _js_element_ids() - _html_element_ids() - _js_created_ids()
    assert not missing, f"main.js references ids absent from index.html: {sorted(missing)}"


def test_no_stale_references_to_the_v1_response_shape():
    """These fields were removed when scoring moved to the LLM schema."""
    for stale in ("data.details", "data.scores", "skills_by_category",
                  "extra_skills", "keyword_similarity"):
        assert stale not in JS, f"main.js still reads the removed field {stale!r}"


def test_model_authored_text_never_reaches_innerhtml():
    """
    An LLM response is untrusted input. Every field the model writes - summary,
    evidence, suggestions, red flags - must be set with textContent.

    Clearing a container (`x.innerHTML = ''`) is the one permitted assignment.
    """
    assignments = re.findall(r"^\s*(?:[\w.]+)\.innerHTML\s*=\s*(.+)$", JS, re.MULTILINE)
    unsafe = [a.strip() for a in assignments if a.strip() not in ("'';", '"";')]
    assert not unsafe, f"unsafe innerHTML assignments: {unsafe}"


@pytest.mark.parametrize("field", [
    "overall_score", "verdict", "summary", "skills", "experience",
    "education", "domain_relevance", "skill_evidence", "matched_skills",
    "missing_skills", "transferable_skills", "red_flags", "suggestions",
])
def test_api_returns_every_field_the_script_reads(client, resume_upload, job_description, field):
    response = client.post(
        "/analyze",
        data={"resume": resume_upload(), "job_description": job_description},
        content_type="multipart/form-data",
    )
    assert field in response.get_json()["result"]


def test_client_and_server_agree_on_the_minimum_job_description():
    """A mismatch means the browser rejects input the server would accept, or worse."""
    from resumescreener.routes import MIN_JD_CHARS

    client_side = int(re.search(r"jd\.length < (\d+)", JS).group(1))
    assert client_side == MIN_JD_CHARS


def test_only_the_baseline_is_labelled_keyword_baseline():
    """
    A live run mislabelled a Gemini result as "Keyword baseline" because the
    badge branched on `=== 'claude'`. Provenance is the whole point of that
    badge, so it must branch on the baseline, not on a provider allowlist.
    """
    assert "payload.engine === 'baseline'" in JS
    assert "payload.engine === 'claude'" not in JS


def test_no_orphaned_result_containers():
    """
    Every container the results view renders into must be referenced somewhere in
    main.js. Two v1 cards survived the rewrite as permanently empty boxes, which
    looked like a broken page to anyone using it.

    Ids reach the script two ways - `getElementById('x')` and as a string
    argument like `renderSkillTags('x', ...)` - so this matches either.
    """
    containers = set(re.findall(r'id="([a-z-]*(?:details|list|skills))"', HTML))
    orphans = {cid for cid in containers if f"'{cid}'" not in JS}
    assert not orphans, f"markup with no renderer in main.js: {sorted(orphans)}"
