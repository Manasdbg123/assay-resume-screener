"""
Tests for browsing the job index and looking up any company on demand.

Every job board is stubbed: `_get` is replaced with a router over canned payloads
shaped like each ATS's real response, so nothing here touches the network.
"""

from datetime import UTC, datetime, timedelta

import pytest
import requests

from resumescreener.jobs import lookup as lookup_module
from resumescreener.jobs.lookup import (
    CompanyLookupError,
    candidate_slugs,
    lookup_company,
    parse_board_url,
)
from resumescreener.jobs.schemas import JobPosting
from resumescreener.jobs.sources import (
    ArbeitnowSource,
    BoardNotFound,
    build_sources,
    fetch_company_board,
)
from resumescreener.jobs.store import JobStore

# --- canned ATS payloads -----------------------------------------------------

ASHBY = {"jobs": [{
    "id": "a1", "title": "ML Engineer", "location": "Remote - EU", "isRemote": True,
    "isListed": True, "publishedAt": "2026-09-01T09:00:00.000+02:00",
    "jobUrl": "https://jobs.ashbyhq.com/acme/a1", "department": "Research",
    "descriptionPlain": "Train models in PyTorch.",
    "compensation": {"compensationTierSummary": "€90K – €120K"},
}, {
    "id": "a2", "title": "Hidden role", "isListed": False, "descriptionPlain": "x",
}]}

WORKABLE = {"name": "Acme Robotics", "jobs": [{
    "shortcode": "W1", "title": "Robotics Engineer", "city": "Berlin", "country": "Germany",
    "telecommuting": False, "url": "https://apply.workable.com/acme/j/W1",
    "published_on": "2026-08-20", "description": "<p>ROS and C++</p>",
    "department": "Hardware", "employment_type": "Full-time",
}]}

SMARTRECRUITERS_LIST = {"totalFound": 1, "content": [{
    "id": "s1", "name": "Data Analyst", "releasedDate": "2026-08-15T12:00:00.000Z",
    "company": {"identifier": "Acme", "name": "Acme Corp"},
    "location": {"city": "Pune", "country": "in", "remote": False},
    "department": {"label": "Analytics"},
}]}
SMARTRECRUITERS_DETAIL = {"jobAd": {"sections": {
    "jobDescription": {"text": "<p>SQL and dashboards</p>"},
    "qualifications": {"text": "<ul><li>3 years SQL</li></ul>"},
}}}

RECRUITEE = {"offers": [{
    "id": 7, "title": "Support Lead", "location": "Amsterdam, Netherlands", "remote": False,
    "careers_url": "https://acme.recruitee.com/o/support-lead",
    "published_at": "2026-08-10 10:00:00 UTC", "description": "<p>Lead support</p>",
    "requirements": "<ul><li>Zendesk</li></ul>",
}]}

GREENHOUSE = {"jobs": [{
    "id": 1, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
    "updated_at": "2026-09-10T00:00:00Z", "location": {"name": "Bengaluru"},
    "content": "<p>Go and Kafka</p>", "company_name": "Acme",
}, {
    "id": 2, "title": "Frontend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
    "updated_at": "2026-09-11T00:00:00Z", "location": {"name": "Remote"},
    "content": "<p>React</p>", "company_name": "Acme",
}]}


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status}", response=response)


def _router(routes: dict):
    """A fake `_get` that answers by URL prefix and 404s everything else."""
    calls = []

    def fake_get(url, params=None):
        calls.append(url)
        for prefix, payload in routes.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return payload() if callable(payload) else payload
        raise _http_error(404)

    fake_get.calls = calls
    return fake_get


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs.db")


# --- ATS parsers -------------------------------------------------------------

def test_ashby_parses_and_skips_unlisted_roles(monkeypatch):
    monkeypatch.setattr("resumescreener.jobs.sources._get",
                        _router({"https://api.ashbyhq.com/": ASHBY}))
    jobs = fetch_company_board("ashby", "acme", "Acme")
    assert [j.id for j in jobs] == ["ashby:acme:a1"]
    job = jobs[0]
    assert job.remote is True
    assert job.company == "Acme"
    assert "€90K – €120K" in job.tags
    assert job.posted_at.tzinfo is not None


def test_workable_uses_the_account_name(monkeypatch):
    monkeypatch.setattr("resumescreener.jobs.sources._get",
                        _router({"https://apply.workable.com/": WORKABLE}))
    [job] = fetch_company_board("workable", "acme")
    assert job.company == "Acme Robotics"
    assert job.location == "Berlin, Germany"
    assert "ROS and C++" in job.description


def test_smartrecruiters_fetches_the_description_per_posting(monkeypatch):
    monkeypatch.setattr("resumescreener.jobs.sources._get", _router({
        "https://api.smartrecruiters.com/v1/companies/Acme/postings/s1": SMARTRECRUITERS_DETAIL,
        "https://api.smartrecruiters.com/v1/companies/Acme/postings": SMARTRECRUITERS_LIST,
    }))
    [job] = fetch_company_board("smartrecruiters", "Acme")
    assert job.company == "Acme Corp"
    assert job.url == "https://jobs.smartrecruiters.com/Acme/s1"
    assert "SQL and dashboards" in job.description
    assert "- 3 years SQL" in job.description


def test_smartrecruiters_empty_listing_means_no_such_company(monkeypatch):
    monkeypatch.setattr("resumescreener.jobs.sources._get", _router({
        "https://api.smartrecruiters.com/": {"totalFound": 0, "content": []},
    }))
    with pytest.raises(BoardNotFound):
        fetch_company_board("smartrecruiters", "Nobody")


def test_recruitee_joins_description_and_requirements(monkeypatch):
    monkeypatch.setattr("resumescreener.jobs.sources._get",
                        _router({"https://acme.recruitee.com/": RECRUITEE}))
    [job] = fetch_company_board("recruitee", "acme")
    assert "Lead support" in job.description
    assert "- Zendesk" in job.description


def test_a_404_is_reported_as_board_not_found(monkeypatch):
    monkeypatch.setattr("resumescreener.jobs.sources._get", _router({}))
    with pytest.raises(BoardNotFound):
        fetch_company_board("greenhouse", "nobody")


@pytest.mark.parametrize("slug", ["evil.com/x", "../admin", "a b", "", "x" * 200])
def test_unsafe_slugs_never_reach_a_url(monkeypatch, slug):
    """A slug becomes part of a URL (and a subdomain for Recruitee)."""
    monkeypatch.setattr("resumescreener.jobs.sources._get", _router({}))
    with pytest.raises(ValueError):
        fetch_company_board("recruitee", slug)


def test_arbeitnow_pages_until_there_is_no_next_link(monkeypatch):
    pages = iter([
        {"data": [{"slug": "a", "title": "Dev", "company_name": "X", "description": "<p>Go</p>",
                   "remote": True, "url": "u", "tags": ["go"], "job_types": [],
                   "location": "Berlin", "created_at": 1_788_000_000}],
         "links": {"next": "page2"}},
        {"data": [{"slug": "b", "title": "Ops", "company_name": "Y", "description": "<p>K8s</p>",
                   "remote": False, "url": "u", "tags": [], "job_types": [],
                   "location": "Munich", "created_at": 1_788_000_000}],
         "links": {"next": None}},
    ])
    monkeypatch.setattr("resumescreener.jobs.sources._get", lambda *a, **k: next(pages))
    jobs = ArbeitnowSource(pages=5).fetch()
    assert [j.id for j in jobs] == ["arbeitnow:a", "arbeitnow:b"]
    assert jobs[0].remote and not jobs[1].remote


def test_build_sources_covers_every_configured_ats():
    sources = build_sources({"greenhouse": ["a"], "ashby": ["b"], "lever": [], "nope": ["c"]},
                            use_aggregators=False)
    assert sorted(s.name for s in sources) == ["ashby", "greenhouse"]


# --- lookup inputs -----------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://boards.greenhouse.io/stripe", ("greenhouse", "stripe")),
    ("https://job-boards.greenhouse.io/anthropic/jobs/123", ("greenhouse", "anthropic")),
    ("https://boards.greenhouse.io/embed/job_board?for=figma", ("greenhouse", "figma")),
    ("jobs.lever.co/plaid", ("lever", "plaid")),
    ("https://jobs.ashbyhq.com/openai/abc", ("ashby", "openai")),
    ("https://apply.workable.com/huggingface/", ("workable", "huggingface")),
    ("https://jobs.smartrecruiters.com/Visa/744000", ("smartrecruiters", "Visa")),
    ("https://acme.recruitee.com/o/role", ("recruitee", "acme")),
    ("https://example.com/careers", None),
    ("Stripe", None),
])
def test_parse_board_url(url, expected):
    assert parse_board_url(url) == expected


def test_candidate_slugs_drop_legal_suffixes_and_keep_casing_variant():
    assert candidate_slugs("Hugging Face, Inc.") == [
        "huggingface", "hugging-face", "HuggingFace", "hugging",
    ]
    assert candidate_slugs("stripe") == ["stripe", "Stripe"]


# --- lookup end to end ---------------------------------------------------------

def test_lookup_finds_a_company_on_whichever_ats_hosts_it(monkeypatch, store):
    fake = _router({"https://boards-api.greenhouse.io/v1/boards/acme/": GREENHOUSE})
    monkeypatch.setattr("resumescreener.jobs.sources._get", fake)

    result = lookup_company("Acme", store)
    assert result.found and not result.cached
    assert [(b.ats, b.slug, b.jobs) for b in result.boards] == [("greenhouse", "acme", 2)]
    assert store.count() == 2
    # Every ATS was asked about every slug variant, in one parallel sweep.
    assert len(fake.calls) >= 6


def test_a_repeat_lookup_is_served_from_the_cache(monkeypatch, store):
    fake = _router({"https://boards-api.greenhouse.io/v1/boards/acme/": GREENHOUSE})
    monkeypatch.setattr("resumescreener.jobs.sources._get", fake)
    lookup_company("Acme", store)
    before = len(fake.calls)

    again = lookup_company("  ACME ", store)
    assert again.cached
    assert len(again.postings) == 2
    assert len(fake.calls) == before


def test_refresh_removes_roles_that_have_closed(monkeypatch, store):
    payload = {"jobs": list(GREENHOUSE["jobs"])}
    monkeypatch.setattr("resumescreener.jobs.sources._get",
                        _router({"https://boards-api.greenhouse.io/v1/boards/acme/":
                                 lambda: payload}))
    lookup_company("Acme", store)
    assert store.count() == 2

    payload["jobs"] = payload["jobs"][:1]      # one role was filled
    result = lookup_company("Acme", store, refresh=True)
    assert len(result.postings) == 1
    assert store.count() == 1


def test_one_failing_ats_does_not_sink_the_lookup(monkeypatch, store):
    monkeypatch.setattr("resumescreener.jobs.sources._get", _router({
        "https://boards-api.greenhouse.io/v1/boards/acme/": GREENHOUSE,
        "https://api.lever.co/": RuntimeError("connection reset"),
    }))
    assert lookup_company("Acme", store).found


def test_an_unknown_company_is_not_found(monkeypatch, store):
    monkeypatch.setattr("resumescreener.jobs.sources._get", _router({}))
    result = lookup_company("Definitely Not Real Co", store)
    assert not result.found and result.postings == []


def test_lookup_rejects_empty_input(store):
    with pytest.raises(CompanyLookupError):
        lookup_company(" ", store)
    with pytest.raises(CompanyLookupError):
        lookup_company("!!", store)


def test_a_careers_url_is_probed_directly(monkeypatch, store):
    fake = _router({"https://boards-api.greenhouse.io/v1/boards/acme/": GREENHOUSE})
    monkeypatch.setattr("resumescreener.jobs.sources._get", fake)
    lookup_company("https://boards.greenhouse.io/acme", store)
    assert fake.calls == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs"]


# --- store search ------------------------------------------------------------

def _job(i, **kw):
    base = {"id": f"t:{i}", "source": "greenhouse", "title": f"Engineer {i}",
            "company": "Acme", "description": "Python services",
            "posted_at": datetime.now(UTC) - timedelta(days=i)}
    base.update(kw)
    return JobPosting(**base)


def test_search_filters_combine(store):
    store.upsert_jobs([
        _job(1, title="Python Developer", location="Pune", remote=False),
        _job(2, title="Go Developer", description="Go only", location="Remote", remote=True),
        _job(3, company="Globex", title="Python Lead", remote=True, source="ashby"),
        _job(40, title="Old Python role"),
    ])
    assert store.search(query="python")[1] == 3
    assert store.search(query="python", remote_only=True)[1] == 1
    assert store.search(company="globex")[1] == 1
    assert store.search(location="pune")[1] == 1
    assert store.search(source="ashby")[1] == 1
    assert store.search(posted_within_days=7)[1] == 3


def test_search_ranks_title_matches_first(store):
    store.upsert_jobs([
        _job(1, title="Data Engineer", description="Uses Python daily"),
        _job(5, title="Python Engineer", description="Services"),
    ])
    jobs, _ = store.search(query="python")
    assert jobs[0].title == "Python Engineer"


def test_search_paginates(store):
    store.upsert_jobs([_job(i) for i in range(1, 11)])
    first, total = store.search(limit=4, offset=0)
    second, _ = store.search(limit=4, offset=4)
    assert total == 10
    assert not {j.id for j in first} & {j.id for j in second}


def test_like_wildcards_in_a_query_are_literal(store):
    store.upsert_jobs([_job(1, title="100% remote"), _job(2, title="Anything")])
    assert store.search(query="%")[1] == 1
    assert store.search(query="_")[1] == 0


def test_companies_are_counted(store):
    store.upsert_jobs([_job(1), _job(2), _job(3, company="Globex")])
    assert store.companies() == [{"company": "Acme", "jobs": 2}, {"company": "Globex", "jobs": 1}]


# --- HTTP ----------------------------------------------------------------------

@pytest.fixture
def jobs_client(tmp_path, config):
    from resumescreener import create_app

    config.job_db_path = str(tmp_path / "jobs.db")
    app = create_app(config)
    app.config["TESTING"] = True
    JobStore(config.job_db_path).upsert_jobs([
        _job(1, title="Python Developer", description="Django and PostgreSQL. " * 30),
        _job(2, title="Go Developer", company="Globex", remote=True),
    ])
    return app.test_client()


def test_browse_endpoint_pages_and_trims_descriptions(jobs_client):
    body = jobs_client.get("/jobs?per_page=1").get_json()
    assert body["total"] == 2 and body["has_more"] is True
    job = body["jobs"][0]
    assert "description" not in job
    assert len(job["snippet"]) <= 262


def test_browse_endpoint_filters(jobs_client):
    body = jobs_client.get("/jobs?remote=true").get_json()
    assert [j["company"] for j in body["jobs"]] == ["Globex"]


def test_browse_endpoint_clamps_bad_paging(jobs_client):
    body = jobs_client.get("/jobs?page=-3&per_page=100000").get_json()
    assert body["page"] == 1 and body["per_page"] == 100


def test_detail_endpoint(jobs_client):
    assert "Django" in jobs_client.get("/jobs/detail?id=t:1").get_json()["description"]
    assert jobs_client.get("/jobs/detail?id=nope").status_code == 404


def test_companies_endpoint(jobs_client):
    body = jobs_client.get("/jobs/companies").get_json()
    assert {c["company"] for c in body["companies"]} == {"Acme", "Globex"}
    assert "ashby" in body["boards"]


def test_lookup_endpoint(jobs_client, monkeypatch):
    monkeypatch.setattr("resumescreener.jobs.sources._get", _router({
        "https://api.ashbyhq.com/posting-api/job-board/initech": ASHBY,
    }))
    body = jobs_client.post("/jobs/lookup", json={"company": "Initech"}).get_json()
    assert body["found"] is True
    assert body["boards"] == [{"ats": "ashby", "slug": "initech", "jobs": 1}]
    assert body["jobs"][0]["company"] == "Initech"


def test_lookup_endpoint_rejects_empty_input(jobs_client):
    assert jobs_client.post("/jobs/lookup", json={"company": ""}).status_code == 400


def test_lookup_module_exports_the_error_type():
    assert issubclass(lookup_module.CompanyLookupError, ValueError)
