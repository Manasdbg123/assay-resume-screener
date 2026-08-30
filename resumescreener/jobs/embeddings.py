"""
Embeddings for semantic job retrieval.

Configuration here was chosen by measurement, not by default. Ranking one CV
against three job descriptions:

    SEMANTIC_SIMILARITY (symmetric)   correct winner, margin 0.0382
    RETRIEVAL_QUERY / _DOCUMENT       correct winner, margin 0.0590

The asymmetric pair separates the right job from the runner-up by 55% more, which
matters because this stage decides which handful of jobs survive to the expensive
LLM stage. A near-tie here means the right job gets cut before anything reasons
about it.

The CV is the query; job descriptions are the documents.
"""

import logging
import time

import numpy as np
from google import genai
from google.genai import errors, types

from ..config import Config
from ..llm import LLMUnavailable

logger = logging.getLogger(__name__)

QUERY_TASK = "RETRIEVAL_QUERY"
DOCUMENT_TASK = "RETRIEVAL_DOCUMENT"

# QUOTA, measured from a 429 response body:
#   EmbedContentRequestsPerDayPerProjectPerModel-FreeTier = 1000 requests/day
# and quota is counted PER ITEM, not per call - a batch of 8 spends 8 units. So
# batching buys latency, never budget. Embedding a 3,900-posting corpus therefore
# takes four days on the free tier, which is exactly why vectors are cached in
# SQLite and never recomputed for unchanged text.
#
# Batch sizing was measured against the live tier, in this order:
#   - batches of 32 full-length postings (~96k tokens): immediate 429
#   - 12 sequential single-item calls at ~1.6s apart: 12/12 succeeded
#   - batches of 1, 2, 4 and 8 real postings after a cooldown: all succeeded
# So the constraint is a short-window burst budget, not request count and not
# batch size. Once tripped it stays tripped for roughly a minute, which is why
# the first batch of a fresh run could fail while single calls worked seconds
# earlier. The answer is to pace, and to back off long enough to outlast the
# window rather than hammering it with 1-2-4 second retries.
DEFAULT_BATCH_SIZE = 8
INTER_BATCH_DELAY_SECONDS = 1.5

# Ingest runs offline in a CronJob, so it can afford to outwait the burst window.
INGEST_RETRY_DELAYS = (10, 25, 45, 70, 90)

# The query path cannot. A user is waiting, and there is a perfectly good TF-IDF
# fallback one exception away - so failing fast and degrading beats spending four
# minutes on retries that probably will not succeed anyway. (Measured: with the
# ingest schedule, a quota-exhausted CV embedding held a request for 240s before
# the fallback ran.)
QUERY_RETRY_DELAYS = (1, 3)
_RETRYABLE_STATUS = {429, 500, 503}


def _build_client(config: Config) -> genai.Client:
    return genai.Client(api_key=config.gemini_api_key)


def _embed_batch(client, model, texts: list[str], task: str,
                 delays: tuple[int, ...]) -> list[list[float]]:
    """Embed one batch, retrying the transient failures the free tier produces."""
    attempts = len(delays)
    for attempt in range(attempts + 1):
        try:
            response = client.models.embed_content(
                model=model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=task),
            )
            return [e.values for e in response.embeddings]
        except (errors.ClientError, errors.ServerError) as exc:
            code = getattr(exc, "code", None)
            retryable = code in _RETRYABLE_STATUS or any(
                str(s) in str(exc) for s in _RETRYABLE_STATUS
            )
            if not retryable or attempt >= attempts:
                raise LLMUnavailable(f"Embedding failed: {exc}") from exc
            delay = delays[min(attempt, len(delays) - 1)]
            logger.warning(
                "embedding batch failed, retrying",
                extra={"attempt": attempt + 1, "delay_s": delay},
            )
            time.sleep(delay)
        except Exception as exc:
            raise LLMUnavailable(f"Could not reach the embedding API: {exc}") from exc
    raise LLMUnavailable("Embedding retries exhausted")


def embed_documents(
    texts: list[str],
    config: Config,
    client=None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[list[float]]:
    """Embed job descriptions. Called during ingest, never on the request path."""
    if not texts:
        return []
    client = client or _build_client(config)

    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        vectors.extend(
            _embed_batch(client, config.embedding_model, batch, DOCUMENT_TASK,
                         INGEST_RETRY_DELAYS)
        )
        logger.info(
            "embedded batch",
            extra={"from": start, "to": start + len(batch), "total": len(texts)},
        )
        if start + batch_size < len(texts):
            time.sleep(INTER_BATCH_DELAY_SECONDS)
    return vectors


def embed_query(text: str, config: Config, client=None) -> np.ndarray:
    """Embed a CV. One call per upload - the only embedding cost at request time."""
    client = client or _build_client(config)
    truncated = text[:config.max_resume_chars]
    vector = _embed_batch(
        client, config.embedding_model, [truncated], QUERY_TASK, QUERY_RETRY_DELAYS
    )[0]
    return np.asarray(vector, dtype=np.float32)


def cosine_similarities(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """
    Cosine similarity of one query against every row of a matrix.

    Normalise then take a single dot product: ranking 5,000 jobs is one matrix
    multiply of a few milliseconds, not 5,000 Python-level comparisons.
    """
    if matrix.size == 0:
        return np.array([])
    query_norm = query / (np.linalg.norm(query) + 1e-10)
    matrix_norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10
    return (matrix / matrix_norms) @ query_norm
