"""
Job ingestion: fetch postings, store them, embed the new ones.

    python -m resumescreener.jobs.ingest                 # fetch + embed
    python -m resumescreener.jobs.ingest --no-embed      # fetch only, no API calls
    python -m resumescreener.jobs.ingest --stats         # what is in the store
    python -m resumescreener.jobs.ingest --embed-only    # embed whatever is missing

Runs offline, never on the request path. In Kubernetes this is a CronJob; locally
it is a command you run when you want fresh postings.

Embedding is incremental by construction: only rows with a NULL embedding are
sent, so re-running after a failed batch resumes rather than restarting, and an
unchanged posting is never re-embedded.

That matters more than it sounds: the Gemini free tier allows 1,000 embed
requests per day and counts them per item, so a few thousand postings take
several days to index. Run this daily and the corpus fills in; the app works
throughout, matching against whatever is already indexed.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from ..config import load_config
from ..llm import LLMUnavailable
from ..logging_config import configure_logging
from .embeddings import embed_documents
from .sources import build_sources
from .store import JobStore

logger = logging.getLogger(__name__)

COMPANIES_FILE = Path(__file__).parent / "companies.json"


def load_companies() -> tuple[list[str], list[str]]:
    data = json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))
    return data.get("greenhouse", []), data.get("lever", [])


def fetch_and_store(store: JobStore, use_remotive: bool = True) -> tuple[int, int]:
    greenhouse, lever = load_companies()
    sources = build_sources(greenhouse, lever, use_remotive=use_remotive)

    all_postings = []
    for source in sources:
        postings = source.fetch()
        print(f"  {source.name:12} {len(postings):>5} postings")
        all_postings.extend(postings)

    if not all_postings:
        return 0, 0
    return store.upsert_jobs(all_postings)


def embed_missing(store: JobStore, config, batch_limit: int = 200) -> int:
    """Embed postings that have no cached vector. Safe to re-run."""
    pending = store.jobs_without_embeddings(limit=batch_limit)
    if not pending:
        print("  nothing to embed - every posting already has a cached vector")
        return 0

    print(f"  embedding {len(pending)} postings...")
    try:
        vectors = embed_documents([p.embedding_text for p in pending], config)
    except LLMUnavailable as exc:
        print(f"  embedding failed: {exc}")
        print("  (re-run later - already-embedded postings are not re-sent)")
        return 0

    store.save_embeddings({p.id: v for p, v in zip(pending, vectors, strict=True)})
    return len(pending)


def main():
    parser = argparse.ArgumentParser(description="Fetch and embed job postings.")
    parser.add_argument("--no-embed", action="store_true", help="fetch only, no API calls")
    parser.add_argument("--embed-only", action="store_true", help="skip fetching")
    parser.add_argument("--no-remotive", action="store_true", help="company boards only")
    parser.add_argument("--limit", type=int, default=200, metavar="N",
                        help="how many postings to embed this run (default 200)")
    parser.add_argument("--stats", action="store_true", help="show store contents and exit")
    parser.add_argument("--prune-days", type=int, metavar="N",
                        help="delete postings not seen for N days")
    args = parser.parse_args()

    config = load_config()
    configure_logging(level=config.log_level, json_logs=False,
                      service=config.service_name, environment=config.environment)
    store = JobStore(config.job_db_path)

    if args.stats:
        stats = store.stats()
        print(f"\njobs in store: {stats['total']}  (embedded: {stats['embedded']})")
        for source, count in sorted(stats["by_source"].items()):
            print(f"  {source:12} {count:>5}")
        return

    if args.prune_days:
        removed = store.delete_older_than(args.prune_days)
        print(f"pruned {removed} stale postings")

    if not args.embed_only:
        print("\nFetching job boards...")
        inserted, updated = fetch_and_store(store, use_remotive=not args.no_remotive)
        print(f"  stored: {inserted} new, {updated} updated")

    if not args.no_embed:
        if not config.gemini_api_key:
            print("\nGEMINI_API_KEY is not set - skipping embeddings.")
            print("Postings are stored; run --embed-only once a key is configured.")
            sys.exit(0)
        print("\nEmbedding...")
        embedded = embed_missing(store, config, batch_limit=args.limit)
        print(f"  embedded: {embedded}")

    stats = store.stats()
    print(f"\nstore now holds {stats['total']} postings, {stats['embedded']} ready for matching")


if __name__ == "__main__":
    main()
