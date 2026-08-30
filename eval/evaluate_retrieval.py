"""
Retrieval evaluation: does the ranker actually surface the right job?

    python -m eval.evaluate_retrieval                  # hybrid vs tfidf
    python -m eval.evaluate_retrieval --json out.json

The setup
---------
The three job descriptions in `eval/data/jobs.json` are the *targets*. They get
mixed into a corpus of real postings from the live job store, and for each
labelled candidate we ask: given only this CV, does the ranker surface the job we
know they are right for?

Only candidates labelled `advance` are scored. A candidate the human rejected has
no "correct" job in the corpus, so including them would measure nothing.

**Distractors are mined, not sampled.** The first version of this harness drew
300 arbitrary postings and both rankers scored a perfect 1.0 - not because they
were perfect, but because the targets were the only documents in the corpus that
did not come from one company. They stood out for reasons unrelated to matching,
and a benchmark that cannot separate two rankers measures nothing. Distractors
are now the real postings *most similar to each target*: the hardest available
negatives, drawn from the corpus a real user is searched against.

The metrics
-----------
    recall@k        Fraction of candidates whose correct job appears in the top k.
                    recall@10 matters most, because the funnel shortlists 30 and
                    LLM-scores the top 10 - a target at rank 40 is never reasoned
                    about at all.
    MRR             Mean reciprocal rank. Rewards ranking the target 1st over 9th,
                    which recall@10 alone treats as identical.
    forced_choice   Among the three target JDs *only*, is this candidate's own
                    target ranked above the other two? A clean three-way choice
                    with no distractors to hide behind, so chance is 0.333. This
                    is the metric that survives corpus saturation.

The comparison
--------------
`hybrid` (embeddings + skill overlap) against `tfidf` (term overlap only, no API
call). If the hybrid ranker cannot beat TF-IDF, the embedding call is cost with
no benefit and should be removed.
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

import numpy as np

from eval.evaluate import load_dataset
from resumescreener.config import load_config
from resumescreener.jobs.embeddings import (
    cosine_similarities,
    embed_documents,
    embed_query,
)
from resumescreener.jobs.ranking import rank_hybrid, rank_tfidf
from resumescreener.jobs.schemas import JobPosting
from resumescreener.jobs.store import JobStore
from resumescreener.llm import LLMUnavailable

K_VALUES = (1, 5, 10, 30)
DEFAULT_DISTRACTORS = 300


def build_corpus(config, store: JobStore, jobs: dict, distractors: int):
    """
    Assemble the evaluation corpus: the target JDs plus real distractor postings.

    Returns (postings, matrix, target_index_by_job_id).
    """
    pool_postings, pool_matrix = store.load_for_matching(limit=5000)
    if pool_matrix is None or len(pool_postings) < 20:
        sys.exit(
            f"Only {len(pool_postings)} embedded postings in the store - not enough "
            "to evaluate retrieval.\nRun: python -m resumescreener.jobs.ingest"
        )

    targets = [
        JobPosting(
            id=f"target:{job_id}",
            source="eval",
            title=text.strip().splitlines()[0],
            company="Evaluation Set",
            description=text,
        )
        for job_id, text in jobs.items()
    ]

    print(f"  embedding {len(targets)} target job descriptions...")
    target_vectors = np.asarray(
        embed_documents([t.embedding_text for t in targets], config), dtype=np.float32
    )

    # Hard-negative mining: keep the postings closest to each target, so the
    # ranker must separate genuinely similar jobs rather than spot the odd one
    # out. The budget is split evenly across targets.
    per_target = max(1, distractors // len(targets))
    chosen: list[int] = []
    for vector in target_vectors:
        similarities = cosine_similarities(vector, pool_matrix)
        taken = 0
        for index in np.argsort(-similarities):
            index = int(index)
            if index in chosen:
                continue
            chosen.append(index)
            taken += 1
            if taken >= per_target:
                break

    negatives = [pool_postings[i] for i in chosen]
    negative_matrix = pool_matrix[chosen]

    postings = targets + negatives
    matrix = np.vstack([target_vectors, negative_matrix])
    target_index = {t.id.split(":", 1)[1]: i for i, t in enumerate(targets)}
    return postings, matrix, target_index


def evaluate(ranker_name, resume_text, query_vector, postings, matrix, target_position, top_k):
    """Return the 1-based rank of the target, or None if outside top_k."""
    if ranker_name == "hybrid":
        ranked = rank_hybrid(resume_text, query_vector, postings, matrix, top_k)
    else:
        ranked = rank_tfidf(resume_text, postings, top_k)

    for position, (index, _score) in enumerate(ranked, start=1):
        if index == target_position:
            return position
    return None


def forced_choice(ranker_name, resume_text, query_vector, postings, matrix,
                  target_positions, correct_position):
    """
    Rank the target JDs against each other only - no distractors.

    Corpus saturation cannot rescue a ranker here: three candidates, one right
    answer, chance is 0.333.
    """
    subset = [postings[i] for i in target_positions]
    subset_matrix = matrix[target_positions]

    if ranker_name == "hybrid":
        ranked = rank_hybrid(resume_text, query_vector, subset, subset_matrix, len(subset))
    else:
        ranked = rank_tfidf(resume_text, subset, len(subset))

    return target_positions[ranked[0][0]] == correct_position


def score_metrics(ranks: list[int | None]) -> dict:
    """recall@k and MRR from a list of target ranks (None = not retrieved)."""
    n = len(ranks)
    metrics = {
        f"recall@{k}": round(sum(1 for r in ranks if r is not None and r <= k) / n, 3)
        for k in K_VALUES
    }
    metrics["mrr"] = round(
        statistics.mean([1 / r if r is not None else 0.0 for r in ranks]), 3
    )
    found = [r for r in ranks if r is not None]
    metrics["median_rank"] = statistics.median(found) if found else None
    metrics["n"] = n
    return metrics


def main():
    parser = argparse.ArgumentParser(description="Evaluate job retrieval ranking.")
    parser.add_argument("--distractors", type=int, default=DEFAULT_DISTRACTORS,
                        help=f"real postings mixed in as noise (default {DEFAULT_DISTRACTORS})")
    parser.add_argument("--json", metavar="PATH", help="write full results to JSON")
    args = parser.parse_args()

    config = load_config()
    if not config.gemini_api_key:
        sys.exit("GEMINI_API_KEY is not set - the hybrid ranker needs embeddings.")

    jobs, candidates = load_dataset()
    store = JobStore(config.job_db_path)

    # Only candidates the human said to advance have a "correct" job to find.
    targets = [c for c in candidates if c["human_verdict"] == "advance"]
    print(f"\nEvaluating retrieval on {len(targets)} advance-labelled candidates")

    postings, matrix, target_index = build_corpus(config, store, jobs, args.distractors)
    print(f"  corpus: {len(postings)} postings "
          f"({len(target_index)} targets + {len(postings) - len(target_index)} real distractors)\n")

    top_k = max(K_VALUES)
    results: dict[str, list] = {"hybrid": [], "tfidf": []}
    forced: dict[str, list] = {"hybrid": [], "tfidf": []}
    rows = []
    all_target_positions = sorted(target_index.values())

    for candidate in targets:
        try:
            query_vector = embed_query(candidate["resume"], config)
        except LLMUnavailable as exc:
            sys.exit(f"Could not embed CV {candidate['id']}: {exc}")

        target_position = target_index[candidate["job_id"]]
        row = {"id": candidate["id"], "job_id": candidate["job_id"]}

        for ranker in ("hybrid", "tfidf"):
            rank = evaluate(ranker, candidate["resume"], query_vector,
                            postings, matrix, target_position, top_k)
            results[ranker].append(rank)
            row[ranker] = rank

            correct = forced_choice(ranker, candidate["resume"], query_vector,
                                    postings, matrix, all_target_positions,
                                    target_position)
            forced[ranker].append(correct)
            row[f"{ranker}_forced"] = correct

        rows.append(row)
        print(f"  {row['id']:<4} {row['job_id']:<18} "
              f"hybrid={_fmt(row['hybrid']):<10} tfidf={_fmt(row['tfidf']):<10} "
              f"forced h/t={'Y' if row['hybrid_forced'] else 'N'}"
              f"/{'Y' if row['tfidf_forced'] else 'N'}")

    metrics = {name: score_metrics(ranks) for name, ranks in results.items()}
    for name in metrics:
        metrics[name]["forced_choice"] = (
            round(sum(forced[name]) / len(forced[name]), 3) if forced[name] else None
        )

    print("\n" + "=" * 66)
    print(f"{'METRIC':<18}{'HYBRID':>12}{'TFIDF':>12}")
    print("=" * 66)
    for key in [f"recall@{k}" for k in K_VALUES] + ["mrr", "median_rank", "forced_choice"]:
        h, t = metrics["hybrid"][key], metrics["tfidf"][key]
        print(f"{key:<18}{_fmt(h):>12}{_fmt(t):>12}")
    print("=" * 66)

    delta = metrics["hybrid"]["recall@10"] - metrics["tfidf"]["recall@10"]
    if delta > 0:
        print(f"\nHybrid beats TF-IDF on recall@10 by {delta:+.3f} - the embedding "
              "call earns its cost.")
    elif delta == 0:
        print(f"\nHybrid and TF-IDF tie on recall@10 (MRR {metrics['hybrid']['mrr']} "
              f"vs {metrics['tfidf']['mrr']}); MRR breaks the tie.")
    else:
        print(f"\nHybrid does NOT beat TF-IDF ({delta:+.3f} recall@10). "
              "The embedding call is not paying for itself on this corpus.")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"metrics": metrics, "per_candidate": rows,
                        "corpus_size": len(postings)}, indent=2),
            encoding="utf-8",
        )
        print(f"\nWrote {args.json}")


def _fmt(value):
    return "not found" if value is None else value


if __name__ == "__main__":
    main()
