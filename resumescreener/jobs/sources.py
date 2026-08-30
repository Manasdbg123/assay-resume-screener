"""
Job board sources.

Every source implements `fetch() -> list[JobPosting]` and nothing else, so adding
a board (Adzuna, Workable, Ashby) means writing one class - the store, the ranker
and the scorer never learn where a posting came from.

All three sources here are public and keyless. Deliberately absent: LinkedIn and
Indeed. Scraping them breaches their terms of service, they block aggressively,
and a portfolio project should not be built on something that can be taken down.
"""

import html
import logging
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime

import requests

from .schemas import JobPosting

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
USER_AGENT = "resume-screener/2.1 (job matching; +https://github.com)"

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


class JobSource(ABC):
    """One job board."""

    name: str

    @abstractmethod
    def fetch(self) -> list[JobPosting]:
        """Return every posting this source currently offers. Never raises."""


class GreenhouseSource(JobSource):
    """
    Greenhouse company boards.

    Public, keyless, and the best text quality of the three - postings come back
    as full HTML job descriptions rather than a summary. Scoped per company, so
    coverage is exactly the company list you give it.
    """

    name = "greenhouse"
    ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

    def __init__(self, company_tokens: list[str]):
        self.company_tokens = company_tokens

    def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for token in self.company_tokens:
            try:
                data = _get(self.ENDPOINT.format(token=token), {"content": "true"})
            except Exception as exc:
                # One dead board must not abort the whole ingest run.
                logger.warning(
                    "greenhouse board fetch failed",
                    extra={"company": token, "reason": str(exc)[:200]},
                )
                continue

            for job in data.get("jobs", []):
                description = _strip_html(job.get("content", ""))
                if not description:
                    continue
                postings.append(JobPosting(
                    id=f"greenhouse:{token}:{job['id']}",
                    source=self.name,
                    title=job.get("title", "").strip(),
                    company=token.replace("-", " ").title(),
                    location=(job.get("location") or {}).get("name", ""),
                    url=job.get("absolute_url", ""),
                    description=_truncate(description),
                    posted_at=_parse_date(job.get("updated_at")),
                    remote="remote" in ((job.get("location") or {}).get("name", "")).lower(),
                ))
            logger.info("fetched greenhouse board", extra={"company": token})
        return postings


class LeverSource(JobSource):
    """Lever company boards. Same model as Greenhouse: public, keyless, per company."""

    name = "lever"
    ENDPOINT = "https://api.lever.co/v0/postings/{token}"

    def __init__(self, company_tokens: list[str]):
        self.company_tokens = company_tokens

    def fetch(self) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for token in self.company_tokens:
            try:
                data = _get(self.ENDPOINT.format(token=token), {"mode": "json"})
            except Exception as exc:
                logger.warning(
                    "lever board fetch failed",
                    extra={"company": token, "reason": str(exc)[:200]},
                )
                continue

            for job in data if isinstance(data, list) else []:
                description = _strip_html(
                    job.get("descriptionPlain") or job.get("description", "")
                )
                # Lever splits requirements into separate list blocks; without
                # them the posting loses exactly the part the scorer needs.
                for section in job.get("lists", []) or []:
                    description += "\n\n" + _strip_html(
                        f"{section.get('text','')}<br>{section.get('content','')}"
                    )
                if not description.strip():
                    continue

                categories = job.get("categories") or {}
                postings.append(JobPosting(
                    id=f"lever:{token}:{job['id']}",
                    source=self.name,
                    title=job.get("text", "").strip(),
                    company=token.replace("-", " ").title(),
                    location=categories.get("location", "") or "",
                    url=job.get("hostedUrl", ""),
                    description=_truncate(description.strip()),
                    posted_at=_parse_epoch_ms(job.get("createdAt")),
                    remote="remote" in (categories.get("location", "") or "").lower(),
                    tags=[t for t in [categories.get("team"), categories.get("commitment")] if t],
                ))
            logger.info("fetched lever board", extra={"company": token})
        return postings


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


def build_sources(
    greenhouse_companies: list[str],
    lever_companies: list[str],
    use_remotive: bool = True,
) -> list[JobSource]:
    """Assemble the configured sources."""
    sources: list[JobSource] = []
    if greenhouse_companies:
        sources.append(GreenhouseSource(greenhouse_companies))
    if lever_companies:
        sources.append(LeverSource(lever_companies))
    if use_remotive:
        sources.append(RemotiveSource())
    return sources
