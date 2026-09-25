"""
Job board sources.

Two shapes of source live here:

* **Company boards** - Greenhouse, Lever, Ashby, Workable, SmartRecruiters and
  Recruitee. Together these applicant-tracking systems host the careers pages of
  tens of thousands of companies, and every one of them exposes a public, keyless
  JSON feed per company. `fetch_company_board(ats, slug)` reads one; the
  `CompanyBoardSource` wraps a list of slugs for batch ingest, and
  `lookup.py` probes all six to find *any* company by name on demand.
* **Aggregators** - Remotive and Arbeitnow. One keyless endpoint each, returning
  postings across many companies at once.

Every source maps into the same `JobPosting`, so the store, the ranker and the
scorer never learn where a posting came from.

Deliberately absent: LinkedIn and Indeed. Scraping them breaches their terms of
service, they block aggressively, and a project should not be built on something
that can be taken down.
"""

import html
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import requests

from .schemas import JobPosting

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
USER_AGENT = "resume-screener/2.2 (job matching; +https://github.com)"

# A company slug goes into a URL path - and, for Recruitee, a subdomain - so it is
# held to a strict alphabet. Anything else could point a request at another host.
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")

# SmartRecruiters lists postings without their text; each description is one more
# request. This caps that fan-out per company.
SMARTRECRUITERS_DETAIL_LIMIT = 60

# Postings vary wildly in length; anything past this is boilerplate about benefits
# and equal-opportunity statements, which only dilutes the embedding.
MAX_DESCRIPTION_CHARS = 12000


def _strip_html(raw: str) -> str:
    """
    Reduce a posting's HTML to readable text.

    Deliberately not a full parser: job boards emit simple markup, and pulling in
    BeautifulSoup for `<p>` and `<li>` is not worth the dependency. List items
    become dashes so requirement bullets survive - the scorer reads those closely.
    """
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/tr)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _truncate(text: str) -> str:
    return text[:MAX_DESCRIPTION_CHARS] if len(text) > MAX_DESCRIPTION_CHARS else text


def _get(url: str, params: dict | None = None) -> dict | list:
    response = requests.get(
        url, params=params, timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


class BoardNotFound(Exception):
    """The ATS has no board under this slug - a normal outcome when probing."""


def _display_name(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def _is_remote(*values) -> bool:
    return any("remote" in (v or "").lower() for v in values if isinstance(v, str))


def _join_location(*parts) -> str:
    return ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


# --- one fetcher per applicant-tracking system ---------------------------------
#
# Each takes a company slug and returns that company's postings. Unlike the batch
# sources they *raise* on failure - lookup needs to tell "this ATS has no such
# company" apart from "this company has no open roles".

def fetch_greenhouse(slug: str, company: str | None = None) -> list[JobPosting]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", {"content": "true"})
    postings = []
    for job in data.get("jobs", []):
        description = _strip_html(job.get("content", ""))
        location = (job.get("location") or {}).get("name", "")
        postings.append(JobPosting(
            id=f"greenhouse:{slug}:{job['id']}",
            source="greenhouse",
            title=job.get("title", "").strip(),
            company=job.get("company_name") or company or _display_name(slug),
            location=location,
            url=job.get("absolute_url", ""),
            description=_truncate(description),
            posted_at=_parse_date(job.get("first_published") or job.get("updated_at")),
            remote=_is_remote(location),
            tags=[d.get("name") for d in job.get("departments", []) or [] if d.get("name")][:4],
        ))
    return postings


def fetch_lever(slug: str, company: str | None = None) -> list[JobPosting]:
    data = _get(f"https://api.lever.co/v0/postings/{slug}", {"mode": "json"})
    postings = []
    for job in data if isinstance(data, list) else []:
        description = _strip_html(job.get("descriptionPlain") or job.get("description", ""))
        # Lever splits requirements into separate list blocks; without them the
        # posting loses exactly the part the scorer needs.
        for section in job.get("lists", []) or []:
            description += "\n\n" + _strip_html(
                f"{section.get('text','')}<br>{section.get('content','')}"
            )
        categories = job.get("categories") or {}
        location = categories.get("location", "") or ""
        postings.append(JobPosting(
            id=f"lever:{slug}:{job['id']}",
            source="lever",
            title=job.get("text", "").strip(),
            company=company or _display_name(slug),
            location=location,
            url=job.get("hostedUrl", ""),
            description=_truncate(description.strip()),
            posted_at=_parse_epoch_ms(job.get("createdAt")),
            remote=_is_remote(location, job.get("workplaceType")),
            tags=[t for t in [categories.get("team"), categories.get("commitment")] if t],
        ))
    return postings


def fetch_ashby(slug: str, company: str | None = None) -> list[JobPosting]:
    data = _get(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        {"includeCompensation": "true"},
    )
    postings = []
    for job in data.get("jobs", []) or []:
        if job.get("isListed") is False:
            continue
        description = job.get("descriptionPlain") or _strip_html(job.get("descriptionHtml", ""))
        location = job.get("location", "") or ""
        comp = (job.get("compensation") or {}).get("compensationTierSummary")
        postings.append(JobPosting(
            id=f"ashby:{slug}:{job['id']}",
            source="ashby",
            title=job.get("title", "").strip(),
            company=company or _display_name(slug),
            location=location,
            url=job.get("jobUrl") or job.get("applyUrl", ""),
            description=_truncate(description.strip()),
            posted_at=_parse_date(job.get("publishedAt")),
            remote=bool(job.get("isRemote")) or _is_remote(location, job.get("workplaceType")),
            tags=[t for t in [job.get("department"), job.get("employmentType"), comp] if t][:4],
        ))
    return postings


def fetch_workable(slug: str, company: str | None = None) -> list[JobPosting]:
    data = _get(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}", {"details": "true"}
    )
    name = data.get("name") or company or _display_name(slug)
    postings = []
    for job in data.get("jobs", []) or []:
        location = _join_location(job.get("city"), job.get("state"), job.get("country"))
        postings.append(JobPosting(
            id=f"workable:{slug}:{job.get('shortcode') or job.get('id')}",
            source="workable",
            title=job.get("title", "").strip(),
            company=name,
            location=location,
            url=job.get("url") or job.get("shortlink", ""),
            description=_truncate(_strip_html(job.get("description", ""))),
            posted_at=_parse_date(job.get("published_on") or job.get("created_at")),
            remote=bool(job.get("telecommuting")) or _is_remote(location),
            tags=[t for t in [job.get("department"), job.get("employment_type")] if t],
        ))
    return postings


def fetch_smartrecruiters(slug: str, company: str | None = None) -> list[JobPosting]:
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    listings: list[dict] = []
    offset = 0
    while True:
        page = _get(base, {"limit": 100, "offset": offset})
        content = page.get("content", []) or []
        listings.extend(content)
        offset += len(content)
        if not content or offset >= int(page.get("totalFound", 0)) or offset >= 1000:
            break
    # An unknown company returns 200 with nothing in it, not a 404.
    if not listings:
        raise BoardNotFound(slug)

    def detail(posting_id: str) -> str:
        try:
            ad = _get(f"{base}/{posting_id}").get("jobAd", {}).get("sections", {})
        except Exception:
            return ""
        return "\n\n".join(
            _strip_html(ad.get(key, {}).get("text", ""))
            for key in ("jobDescription", "qualifications", "additionalInformation")
        ).strip()

    head = listings[:SMARTRECRUITERS_DETAIL_LIMIT]
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = dict(zip(
            [j["id"] for j in head], pool.map(detail, [j["id"] for j in head]), strict=True
        ))

    postings = []
    for job in listings:
        loc = job.get("location") or {}
        location = loc.get("fullLocation") or _join_location(
            loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()
        )
        meta = [(job.get(k) or {}).get("label") for k in
                ("department", "function", "typeOfEmployment", "experienceLevel")]
        description = texts.get(job["id"]) or (
            f"{job.get('name', '')}\n" + "\n".join(f"- {m}" for m in meta if m)
        )
        identifier = (job.get("company") or {}).get("identifier") or slug
        postings.append(JobPosting(
            id=f"smartrecruiters:{slug}:{job['id']}",
            source="smartrecruiters",
            title=job.get("name", "").strip(),
            company=(job.get("company") or {}).get("name") or company or _display_name(slug),
            location=location,
            url=f"https://jobs.smartrecruiters.com/{identifier}/{job['id']}",
            description=_truncate(description.strip()),
            posted_at=_parse_date(job.get("releasedDate")),
            remote=bool(loc.get("remote")) or _is_remote(location),
            tags=[m for m in meta if m][:4],
        ))
    return postings


def fetch_recruitee(slug: str, company: str | None = None) -> list[JobPosting]:
    data = _get(f"https://{slug.lower()}.recruitee.com/api/offers/")
    postings = []
    for job in data.get("offers", []) or []:
        description = _strip_html(
            f"{job.get('description', '')}<br>{job.get('requirements', '')}"
        )
        location = job.get("location") or _join_location(job.get("city"), job.get("country"))
        postings.append(JobPosting(
            id=f"recruitee:{slug}:{job['id']}",
            source="recruitee",
            title=job.get("title", "").strip(),
            company=job.get("company_name") or company or _display_name(slug),
            location=location,
            url=job.get("careers_url") or job.get("careers_apply_url", ""),
            description=_truncate(description),
            posted_at=_parse_date(job.get("published_at") or job.get("created_at")),
            remote=bool(job.get("remote")) or _is_remote(location),
            tags=[t for t in [job.get("department"), job.get("employment_type_code")] if t],
        ))
    return postings


ATS_FETCHERS: dict[str, Callable[..., list[JobPosting]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
}


def fetch_company_board(ats: str, slug: str, company: str | None = None) -> list[JobPosting]:
    """
    Every current posting one company has on one ATS.

    `company` is a fallback display name, used when the ATS does not report one.

    Raises BoardNotFound when the ATS has no such company, so a caller probing
    several systems can tell a miss from an empty board. Postings with no text
    are dropped - they cannot be screened or matched.
    """
    if ats not in ATS_FETCHERS:
        raise ValueError(f"unknown ATS: {ats}")
    if not SLUG_PATTERN.match(slug):
        raise ValueError(f"invalid company slug: {slug!r}")
    try:
        postings = ATS_FETCHERS[ats](slug, company)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (400, 404, 410):
            raise BoardNotFound(slug) from exc
        raise
    return [p for p in postings if p.title and p.description]


class JobSource(ABC):
    """One job board, fetched in bulk for ingest."""

    name: str

    @abstractmethod
    def fetch(self) -> list[JobPosting]:
        """Return every posting this source currently offers. Never raises."""


class CompanyBoardSource(JobSource):
    """A list of companies on one ATS. A dead slug is logged and skipped."""

    def __init__(self, ats: str, company_tokens: list[str]):
        self.name = ats
        self.company_tokens = company_tokens

    def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for token in self.company_tokens:
            try:
                found = fetch_company_board(self.name, token)
            except Exception as exc:
                # One dead board must not abort the whole ingest run.
                logger.warning(
                    f"{self.name} board fetch failed",
                    extra={"company": token, "reason": str(exc)[:200]},
                )
                continue
            postings.extend(found)
            logger.info(f"fetched {self.name} board",
                        extra={"company": token, "postings": len(found)})
        return postings


class GreenhouseSource(CompanyBoardSource):
    """
    Greenhouse company boards. The best text quality of the six - postings come
    back as full HTML job descriptions rather than a summary.
    """

    def __init__(self, company_tokens: list[str]):
        super().__init__("greenhouse", company_tokens)


class LeverSource(CompanyBoardSource):
    def __init__(self, company_tokens: list[str]):
        super().__init__("lever", company_tokens)


class RemotiveSource(JobSource):
    """
    Remotive: one keyless endpoint returning remote tech jobs with full
    descriptions. Narrower than the company boards but far broader per request.
    """

    name = "remotive"
    ENDPOINT = "https://remotive.com/api/remote-jobs"

    def __init__(self, categories: list[str] | None = None, limit: int = 200):
        self.categories = categories or ["software-dev", "devops", "data"]
        self.limit = limit

    def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for category in self.categories:
            try:
                data = _get(self.ENDPOINT, {"category": category, "limit": self.limit})
            except Exception as exc:
                logger.warning(
                    "remotive fetch failed",
                    extra={"category": category, "reason": str(exc)[:200]},
                )
                continue

            for job in data.get("jobs", []):
                description = _strip_html(job.get("description", ""))
                if not description:
                    continue
                postings.append(JobPosting(
                    id=f"remotive:{job['id']}",
                    source=self.name,
                    title=job.get("title", "").strip(),
                    company=job.get("company_name", "").strip(),
                    location=job.get("candidate_required_location", "") or "Remote",
                    url=job.get("url", ""),
                    description=_truncate(description),
                    posted_at=_parse_date(job.get("publication_date")),
                    remote=True,
                    tags=job.get("tags", [])[:12],
                ))
            logger.info("fetched remotive category", extra={"category": category})
        return postings


class ArbeitnowSource(JobSource):
    """
    Arbeitnow: a keyless aggregator of company career pages, strongest in Europe.
    Paged; each page is 100 postings.
    """

    name = "arbeitnow"
    ENDPOINT = "https://www.arbeitnow.com/api/job-board-api"

    def __init__(self, pages: int = 5):
        self.pages = pages

    def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for page in range(1, self.pages + 1):
            try:
                data = _get(self.ENDPOINT, {"page": page})
            except Exception as exc:
                logger.warning("arbeitnow fetch failed",
                               extra={"page": page, "reason": str(exc)[:200]})
                break
            jobs = data.get("data", []) or []
            for job in jobs:
                description = _strip_html(job.get("description", ""))
                if not description:
                    continue
                location = job.get("location", "") or ""
                postings.append(JobPosting(
                    id=f"arbeitnow:{job.get('slug')}",
                    source=self.name,
                    title=job.get("title", "").strip(),
                    company=job.get("company_name", "").strip(),
                    location=location,
                    url=job.get("url", ""),
                    description=_truncate(description),
                    posted_at=_parse_epoch_s(job.get("created_at")),
                    remote=bool(job.get("remote")) or _is_remote(location),
                    tags=(job.get("tags", []) + job.get("job_types", []))[:12],
                ))
            if not jobs or not (data.get("links") or {}).get("next"):
                break
        logger.info("fetched arbeitnow", extra={"postings": len(postings)})
        return postings

def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_epoch_ms(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _parse_epoch_s(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def build_sources(
    companies: dict[str, list[str]],
    use_aggregators: bool = True,
) -> list[JobSource]:
    """Assemble the configured sources: one per ATS with companies, plus aggregators."""
    sources: list[JobSource] = [
        CompanyBoardSource(ats, slugs)
        for ats, slugs in companies.items()
        if ats in ATS_FETCHERS and slugs
    ]
    if use_aggregators:
        sources.extend([RemotiveSource(), ArbeitnowSource()])
    return sources
