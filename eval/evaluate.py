"""
Evaluation harness: does the Claude engine actually beat keyword matching?

Run it:
    python -m eval.evaluate --engine baseline          # free, no API key
    python -m eval.evaluate --engine gemini            # needs GEMINI_API_KEY
    python -m eval.evaluate --engine claude            # needs ANTHROPIC_API_KEY
    python -m eval.evaluate --engine all --json out.json

Metrics
-------
Absolute score agreement is the least interesting property - a screener that is
uniformly 10 points low is still perfectly useful. What matters is ORDER: does
the tool rank candidates the way a human recruiter did?

    pairwise_accuracy   For every pair of candidates for the same job where the
                        human separated them, did the engine order them the same
                        way? This is the headline number. 0.5 is a coin flip.
    spearman            Rank correlation against the human scores, per job then
                        averaged. Robust to monotonic miscalibration.
    decision_accuracy   Treating >=65 as "advance", how often does the engine
                        agree with the human call? Borderline candidates are
                        excluded - human reviewers disagree there too.
    mae                 Mean absolute error in raw points. Reported last, because
                        it is the metric most likely to mislead.
"""

import argparse
import json
import statistics
import sys
import time
from itertools import combinations
from pathlib import Path

from resumescreener.baseline import screen_baseline
from resumescreener.config import load_config
from resumescreener.llm import LLMUnavailable

DATA_DIR = Path(__file__).parent / "data"
ADVANCE_THRESHOLD = 65.0
ENGINES = ("baseline", "gemini", "claude")


def _engine_for(name: str):
    """Import a provider engine lazily - neither SDK is required to be installed."""
    if name == "gemini":
        from resumescreener.gemini import screen
    else:
        from resumescreener.llm import screen
    return screen


def load_dataset():
    jobs = json.loads((DATA_DIR / "jobs.json").read_text(encoding="utf-8"))
    lines = (DATA_DIR / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
    candidates = [json.loads(line) for line in lines if line.strip()]
    return jobs, candidates


def _ranks(values: list[float]) -> list[float]:
    """Fractional ranks, averaging ties."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float:
    """Spearman rank correlation, implemented directly to avoid a scipy dependency."""
    if len(a) < 2:
        return float("nan")
    ra, rb = _ranks(a), _ranks(b)
    mean_a, mean_b = statistics.mean(ra), statistics.mean(rb)
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    den = (sum((x - mean_a) ** 2 for x in ra) * sum((y - mean_b) ** 2 for y in rb)) ** 0.5
    return num / den if den else float("nan")


def run_engine(engine: str, jobs: dict, candidates: list[dict]) -> list[dict]:
    """Score every candidate, returning per-row predictions."""
    config = load_config()

    if engine != "baseline":
        # Point the config at whichever provider this run is measuring, so a
        # single process can evaluate both back to back.
        config.llm_provider = engine
        if not config.api_key_for_provider:
            key = "GEMINI_API_KEY" if engine == "gemini" else "ANTHROPIC_API_KEY"
            sys.exit(f"{key} is not set - cannot run the {engine} engine.")
        engine_screen = _engine_for(engine)

    rows = []
    # Grouping by job means consecutive calls share a job description, so the
    # cached JD prefix is actually reused. Shuffling here would cost real money.
    for job_id in sorted({c["job_id"] for c in candidates}):
        jd = jobs[job_id]
        for candidate in [c for c in candidates if c["job_id"] == job_id]:
            started = time.perf_counter()
            if engine == "baseline":
                result = screen_baseline(candidate["resume"], jd)
                meta = {}
            else:
                try:
                    result, meta = engine_screen(candidate["resume"], jd, config)
                except LLMUnavailable as exc:
                    sys.exit(f"{engine} engine failed on {candidate['id']}: {exc}")

            elapsed = int((time.perf_counter() - started) * 1000)
            rows.append({
                "id": candidate["id"],
                "job_id": job_id,
                "human_score": candidate["human_score"],
                "human_verdict": candidate["human_verdict"],
                "predicted_score": round(result.overall_score, 1),
                "predicted_verdict": result.verdict.value,
                "latency_ms": meta.get("latency_ms", elapsed),
                "cache_read_tokens": meta.get("cache_read_tokens", 0),
            })
            print(
                f"  {candidate['id']:<4} human {candidate['human_score']:>3}"
                f"   predicted {result.overall_score:>5.1f}",
                flush=True,
            )
    return rows


def score_metrics(rows: list[dict]) -> dict:
    # --- pairwise ranking, within each job ---
    concordant = comparable = 0
    rhos = []
    for job_id in {r["job_id"] for r in rows}:
        group = [r for r in rows if r["job_id"] == job_id]
        for x, y in combinations(group, 2):
            if x["human_score"] == y["human_score"]:
                continue  # the human expressed no preference
            comparable += 1
            human_order = x["human_score"] > y["human_score"]
            model_order = x["predicted_score"] > y["predicted_score"]
            concordant += human_order == model_order
        if len(group) >= 2:
            rho = spearman(
                [r["human_score"] for r in group],
                [r["predicted_score"] for r in group],
            )
            if rho == rho:  # excludes NaN
                rhos.append(rho)

    # --- advance/reject decision, excluding deliberate borderlines ---
    decisive = [r for r in rows if r["human_verdict"] in ("advance", "reject")]
    correct = sum(
        (r["predicted_score"] >= ADVANCE_THRESHOLD) == (r["human_verdict"] == "advance")
        for r in decisive
    )

    errors = [abs(r["predicted_score"] - r["human_score"]) for r in rows]

    return {
        "n": len(rows),
        "pairwise_accuracy": round(concordant / comparable, 3) if comparable else None,
        "pairwise_comparisons": comparable,
        "spearman": round(statistics.mean(rhos), 3) if rhos else None,
        "decision_accuracy": round(correct / len(decisive), 3) if decisive else None,
        "decision_n": len(decisive),
        "mae": round(statistics.mean(errors), 1),
        "median_latency_ms": int(statistics.median([r["latency_ms"] for r in rows])),
        "cache_read_tokens": sum(r["cache_read_tokens"] for r in rows),
    }


def print_report(results: dict[str, dict]):
    print("\n" + "=" * 78)
    print(f"{'METRIC':<24}" + "".join(f"{name.upper():>16}" for name in results))
    print("=" * 78)
    layout = [
        ("pairwise accuracy", "pairwise_accuracy", "0.5 = chance"),
        ("spearman rho", "spearman", "rank correlation vs. human"),
        ("decision accuracy", "decision_accuracy", "advance/reject agreement"),
        ("mean abs. error", "mae", "points, lower is better"),
        ("median latency (ms)", "median_latency_ms", ""),
    ]
    for label, key, note in layout:
        cells = "".join(
            f"{('-' if m[key] is None else m[key]):>16}" for m in results.values()
        )
        print(f"{label:<24}{cells}   {note}")
    print("=" * 78)

    if "baseline" in results and "claude" in results:
        base, claude = results["baseline"], results["claude"]
        if base["pairwise_accuracy"] and claude["pairwise_accuracy"]:
            delta = claude["pairwise_accuracy"] - base["pairwise_accuracy"]
            verdict = "beats" if delta > 0 else "does NOT beat"
            print(
                f"\nClaude {verdict} the keyword baseline on ranking "
                f"({delta:+.3f} pairwise accuracy)."
            )
        if claude["cache_read_tokens"]:
            print(
                f"Prompt cache served {claude['cache_read_tokens']:,} input tokens "
                f"(~90% cheaper than uncached)."
            )


def main():
    parser = argparse.ArgumentParser(description="Evaluate screening engines.")
    parser.add_argument(
        "--engine",
        choices=[*ENGINES, "both", "all"],
        default="baseline",
        help="'both' = baseline + gemini; 'all' = every engine",
    )
    parser.add_argument("--json", metavar="PATH", help="write full results to JSON")
    args = parser.parse_args()

    jobs, candidates = load_dataset()
    if args.engine == "all":
        engines = list(ENGINES)
    elif args.engine == "both":
        engines = ["baseline", "gemini"]
    else:
        engines = [args.engine]

    results, all_rows = {}, {}
    for engine in engines:
        print(f"\nRunning {engine} engine on {len(candidates)} labelled pairs...")
        rows = run_engine(engine, jobs, candidates)
        all_rows[engine] = rows
        results[engine] = score_metrics(rows)

    print_report(results)

    if args.json:
        Path(args.json).write_text(
            json.dumps({"metrics": results, "predictions": all_rows}, indent=2),
            encoding="utf-8",
        )
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
