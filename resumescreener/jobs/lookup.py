"""
Find every current opening at any company, on demand.

Most companies do not run their own job-board software; they rent it from an
applicant-tracking system. Six of the biggest - Greenhouse, Lever, Ashby,
Workable, SmartRecruiters and Recruitee - each publish a keyless JSON feed per
company, addressed by a slug that is almost always the company name.

So "show me every open role at Acme" becomes: turn the name into a few likely
slugs, ask all six systems about each one in parallel, and keep whatever
answers. A careers-page URL short-circuits the guessing entirely.

Results are written to the job store - replacing that board's previous postings,
so filled roles disappear - and the lookup itself is cached for a few hours so
a second search for the same company costs nothing.

What this cannot reach: companies on Workday, Taleo, iCIMS or a home-grown
careers site. Those have no public feed, and scraping them is out of scope for
the same reasons LinkedIn is.
"""

import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .schemas import JobPosting
from .sources import ATS_FETCHERS, SLUG_PATTERN, BoardNotFound, fetch_company_board
from .store import JobStore

logger = logging.getLogger(__name__)

# Legal-form suffixes that never appear in a board slug.
_SUFFIXES = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "gmbh",
             "plc", "pvt", "private", "sa", "ag", "bv", "the"}

# Careers-page URL patterns -> (ats, slug group). Pasting the URL of a company's
# job board is the one input that needs no guessing at all.
_URL_PATTERNS = [
    ("greenhouse", re.compile(r"^(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io$"), 1),
    ("lever", re.compile(r"^jobs(?:\.eu)?\.lever\.co$"), 1),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com$"), 1),
    ("workable", re.compile(r"^apply\.workable\.com$"), 1),
    ("smartrecruiters", re.compile(r"^(?:jobs|careers)\.smartrecruiters\.com$"), 1),
]


class CompanyLookupError(ValueError):
    """The input cannot be turned into a company to look up."""


@dataclass
class Board:
    ats: str
    slug: str
    jobs: int

    def as_dict(self) -> dict:
        return {"ats": self.ats, "slug": self.slug, "jobs": self.jobs}


@dataclass
class LookupResult:
    query: str
    company: str
    boards: list[Board] = field(default_factory=list)
    postings: list[JobPosting] = field(default_factory=list)
    cached: bool = False

    @property
    def found(self) -> bool:
        return bool(self.boards)


def normalise_query(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def parse_board_url(text: str) -> tuple[str, str] | None:
    """(ats, slug) if `text` is a recognisable job-board URL, else None."""
    candidate = text.strip()
    if "://" not in candidate:
        if "/" not in candidate and ".recruitee.com" not in candidate:
            return None
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    segments = [s for s in parsed.path.split("/") if s]

    match = re.match(r"^([a-z0-9-]+)\.recruitee\.com$", host)
    if match:
        return "recruitee", match.group(1)
    for ats, pattern, index in _URL_PATTERNS:
        if pattern.match(host) and len(segments) >= index:
            slug = segments[index - 1]
            # Greenhouse embeds use ?for=<slug> on a generic path.
            if ats == "greenhouse" and slug == "embed":
                slug = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p).get("for", "")
            if SLUG_PATTERN.match(slug or ""):
                return ats, slug
    return None


def candidate_slugs(name: str) -> list[str]:
    """
    Likely board slugs for a company name, most likely first.

    "Hugging Face, Inc." -> huggingface, hugging-face, HuggingFace. The camel-case
    form is for SmartRecruiters, whose identifiers keep the company's casing.
    """
    words = re.findall(r"[A-Za-z0-9]+", name)
    core = [w for w in words if w.lower() not in _SUFFIXES] or words
    if not core:
        return []
    variants = [
        "".join(w.lower() for w in core),
        "-".join(w.lower() for w in core),
        "".join(w[:1].upper() + w[1:] for w in core),
    ]
    if len(core) > 1:
        variants.append(core[0].lower())   # "Stripe Payments" is usually just "stripe"
    seen: list[str] = []
    for v in variants:
        if SLUG_PATTERN.match(v) and v not in seen:
            seen.append(v)
    return seen[:4]


def _probe(ats: str, slug: str, company: str | None) -> tuple[str, str, list[JobPosting] | None]:
    try:
        return ats, slug, fetch_company_board(ats, slug, company)
    except BoardNotFound:
        return ats, slug, None
    except Exception as exc:
        logger.warning("board probe failed",
                       extra={"ats": ats, "company": slug, "reason": str(exc)[:200]})
        return ats, slug, None


def lookup_company(
    text: str,
    store: JobStore,
    cache_hours: float = 6,
    refresh: bool = False,
    max_workers: int = 12,
) -> LookupResult:
    """
    Every current posting for the company named (or linked) in `text`.

    Raises CompanyLookupError when the input is not usable as a company name.
    """
    text = (text or "").strip()
    if not 2 <= len(text) <= 200:
        raise CompanyLookupError("Enter a company name or a careers-page URL.")
    query = normalise_query(text)

    if not refresh:
        cached = store.recent_lookup(query, cache_hours)
        if cached is not None:
            boards = [Board(**b) for b in cached["boards"]]
            postings = [job for b in boards for job in store.board_jobs(b.ats, b.slug)]
            return LookupResult(query, cached["company"], boards, postings, cached=True)

    direct = parse_board_url(text)
    if direct:
        probes = [direct]
        display = None
    else:
        slugs = candidate_slugs(text)
        if not slugs:
            raise CompanyLookupError("Enter a company name or a careers-page URL.")
        probes = [(ats, slug) for slug in slugs for ats in ATS_FETCHERS]
        # "hugging face" typed in a hurry still displays as "Hugging Face".
        display = text if any(c.isupper() for c in text) else text.title()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(lambda p: _probe(p[0], p[1], display), probes))

    boards: list[Board] = []
    postings: list[JobPosting] = []
    seen_ids: set[str] = set()
    for ats, slug, found in results:
        if found is None:
            continue
        # Clears roles that have closed since the last lookup, even when the
        # board is now empty.
        store.replace_board(ats, slug, found)
        # Two slug variants can resolve to the same board; count it once. An
        # empty board is not reported: some ATSs answer an unknown slug with an
        # empty list rather than a 404, so "0 jobs" cannot be told from "no such
        # company".
        fresh = [p for p in found if p.id not in seen_ids]
        if not fresh:
            continue
        seen_ids.update(p.id for p in fresh)
        boards.append(Board(ats, slug, len(fresh)))
        postings.extend(fresh)

    # The name the boards themselves use, when they report one.
    names = Counter(p.company for p in postings)
    company = names.most_common(1)[0][0] if names else (display or text)
    store.save_lookup(query, company, [b.as_dict() for b in boards])
    logger.info("company lookup", extra={
        "query": query, "boards": [f"{b.ats}:{b.slug}" for b in boards],
        "postings": len(postings),
    })
    return LookupResult(query, company, boards, postings, cached=False)
