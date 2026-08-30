"""
Retrieval ranking: narrow thousands of postings to the handful worth paying an
LLM to read.

Two rankers live here so `eval/evaluate_retrieval.py` can measure them against
each other rather than assuming the fancier one wins:

    tfidf   - the existing baseline engine's term overlap. Free, no API call.
    hybrid  - semantic embedding similarity blended with skill overlap.

Why blend at all? Measured cosine margins between adjacent technical roles are
thin - a backend CV scored 0.777 against a backend JD and 0.718 against a devops
JD. Embeddings capture "this is a senior infrastructure-flavoured engineer" but
blur the specifics; exact skill overlap is brittle about synonyms but razor sharp
about whether the resume literally contains PostgreSQL. Each covers the other's
weakness, so the blend is more robust than either alone.
"""

import logging

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ..baseline import extract_skills
from .schemas import JobPosting

logger = logging.getLogger(__name__)

# Weighting favours the semantic signal, with skill overlap as a sharpening term.
# These are a starting point measured by eval/evaluate_retrieval.py, not physics.
EMBEDDING_WEIGHT = 0.75
SKILL_WEIGHT = 0.25


def skill_overlap_scores(resume_text: str, postings: list[JobPosting]) -> np.ndarray:
    """
    Fraction of each posting's required skills that the resume demonstrates.

    Reuses the baseline engine's dictionary matcher, so the term-boundary and
    longest-match work done there benefits retrieval too.
    """
    resume_skills = extract_skills(resume_text)["found_skills"]
    scores = np.zeros(len(postings), dtype=np.float32)

    for index, posting in enumerate(postings):
        job_skills = extract_skills(posting.searchable_text)["found_skills"]
        if not job_skills:
            # A posting naming no recognisable skills gives no signal either way;
            # a neutral score avoids both rewarding and punishing it.
            scores[index] = 0.5
            continue
        scores[index] = len(resume_skills & job_skills) / len(job_skills)
    return scores


def rank_hybrid(
    resume_text: str,
    query_vector: np.ndarray,
    postings: list[JobPosting],
    matrix: np.ndarray,
    top_k: int,
) -> list[tuple[int, float]]:
    """
    Rank postings by blended semantic and skill-overlap score.

    Returns (index, score) pairs, best first, where index refers to `postings`.
    """
    from .embeddings import cosine_similarities

    semantic = cosine_similarities(query_vector, matrix)
    skills = skill_overlap_scores(resume_text, postings)

    # Cosine similarities across a job corpus occupy a narrow band (roughly
    # 0.6-0.85), so raw values would let the skill term dominate the blend.
    # Min-max scaling within this result set restores comparable ranges.
    spread = semantic.max() - semantic.min()
    normalised = (semantic - semantic.min()) / spread if spread > 1e-6 else np.full_like(semantic, 0.5)

    blended = EMBEDDING_WEIGHT * normalised + SKILL_WEIGHT * skills
    order = np.argsort(-blended)[:top_k]
    return [(int(i), float(blended[i])) for i in order]


def rank_tfidf(
    resume_text: str,
    postings: list[JobPosting],
    top_k: int,
) -> list[tuple[int, float]]:
    """
    Control arm: rank by TF-IDF cosine similarity alone, no embeddings.

    This is what the feature would be without an embedding model, and the number
    the hybrid ranker has to beat in the retrieval evaluation. It is also the
    live fallback when the embedding quota runs out.

    Implementation note: the corpus is vectorised **once**, then compared in a
    single matrix operation. The obvious version - calling the baseline engine's
    pairwise similarity per posting - refits a TfidfVectorizer for every job, and
    measured 288 seconds over 640 postings in a real request. This version does
    the same work in well under a second.
    """
    corpus = [resume_text] + [p.searchable_text for p in postings]
    try:
        vectorizer = TfidfVectorizer(
            stop_words="english", max_features=20000, ngram_range=(1, 2)
        )
        tfidf = vectorizer.fit_transform(corpus)
    except ValueError:
        # An empty or stop-word-only corpus; no ranking signal available.
        return [(i, 0.0) for i in range(min(top_k, len(postings)))]

    scores = cosine_similarity(tfidf[0:1], tfidf[1:]).ravel()
    order = np.argsort(-scores)[:top_k]
    return [(int(i), float(scores[i])) for i in order]
