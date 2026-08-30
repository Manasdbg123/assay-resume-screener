"""
SQLite job store.

Two things live here: the postings themselves, and their embedding vectors.

Caching the embedding alongside the posting is what makes the whole feature
affordable. A job description is embedded **once, ever** - not once per candidate
who searches. With a thousand jobs cached, a new upload costs one embedding call
(the CV) plus a numpy dot product, regardless of how many users have searched
before.

SQLite is the right call at this size: a few thousand postings, one process, no
ops burden. Postgres with pgvector is the move if this ever needs concurrent
writers or approximate nearest-neighbour search over six figures of rows.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .schemas import JobPosting

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT,
    url           TEXT,
    description   TEXT NOT NULL,
    posted_at     TEXT,
    remote        INTEGER DEFAULT 0,
    tags          TEXT,
    fetched_at    TEXT NOT NULL,
    -- float32 vector, stored raw. Small enough at this scale that a blob beats
    -- a dedicated vector store, and it keeps the dependency list short.
    embedding     BLOB,
    embedding_dim INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_source    ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_posted_at ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS idx_jobs_embedded  ON jobs(embedding_dim);
"""


class JobStore:
    """Persistent store for postings and their cached embeddings."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- writes ----------------------------------------------------------

    def upsert_jobs(self, postings: list[JobPosting]) -> tuple[int, int]:
        """
        Insert or update postings.

        Returns (inserted, updated). An existing row keeps its cached embedding
        unless the description actually changed - re-embedding unchanged text is
        the most obvious way to waste quota.
        """
        inserted = updated = 0
        now = datetime.now(UTC).isoformat()

        with self._connect() as conn:
            for job in postings:
                existing = conn.execute(
                    "SELECT description FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()

                if existing is None:
                    conn.execute(
                        """INSERT INTO jobs (id, source, title, company, location, url,
                                             description, posted_at, remote, tags, fetched_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (job.id, job.source, job.title, job.company, job.location,
                         job.url, job.description,
                         job.posted_at.isoformat() if job.posted_at else None,
                         int(job.remote), json.dumps(job.tags), now),
                    )
                    inserted += 1
                    continue

                text_changed = existing["description"] != job.description
                conn.execute(
                    """UPDATE jobs SET source=?, title=?, company=?, location=?, url=?,
                                       description=?, posted_at=?, remote=?, tags=?, fetched_at=?
                       WHERE id=?""",
                    (job.source, job.title, job.company, job.location, job.url,
                     job.description,
                     job.posted_at.isoformat() if job.posted_at else None,
                     int(job.remote), json.dumps(job.tags), now, job.id),
                )
                if text_changed:
                    # The cached vector describes text that no longer exists.
                    conn.execute(
                        "UPDATE jobs SET embedding=NULL, embedding_dim=NULL WHERE id=?",
                        (job.id,),
                    )
                updated += 1

        logger.info("job upsert complete", extra={"inserted": inserted, "updated": updated})
        return inserted, updated

    def save_embeddings(self, vectors: dict[str, list[float]]) -> None:
        """Persist embedding vectors keyed by job id."""
        with self._connect() as conn:
            for job_id, vector in vectors.items():
                array = np.asarray(vector, dtype=np.float32)
                conn.execute(
                    "UPDATE jobs SET embedding=?, embedding_dim=? WHERE id=?",
                    (array.tobytes(), int(array.shape[0]), job_id),
                )

    def delete_older_than(self, days: int) -> int:
        """Drop stale postings. A job board entry from last year is noise."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM jobs WHERE fetched_at < datetime('now', ?)", (f"-{days} days",)
            )
            return cursor.rowcount

    # --- reads -----------------------------------------------------------

    def count(self, embedded_only: bool = False) -> int:
        query = "SELECT COUNT(*) AS n FROM jobs"
        if embedded_only:
            query += " WHERE embedding IS NOT NULL"
        with self._connect() as conn:
            return conn.execute(query).fetchone()["n"]

    def jobs_without_embeddings(self, limit: int = 500) -> list[JobPosting]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE embedding IS NULL LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_posting(row) for row in rows]

    def load_for_matching(
        self,
        remote_only: bool = False,
        sources: list[str] | None = None,
        limit: int = 5000,
    ) -> tuple[list[JobPosting], np.ndarray | None]:
        """
        Load candidate postings and their embedding matrix.

        The matrix comes back as one (n, dim) array so ranking is a single matrix
        multiply rather than n separate cosine calls.
        """
        query = "SELECT * FROM jobs WHERE embedding IS NOT NULL"
        params: list = []
        if remote_only:
            query += " AND remote = 1"
        if sources:
            query += f" AND source IN ({','.join('?' * len(sources))})"
            params.extend(sources)
        query += " ORDER BY COALESCE(posted_at, fetched_at) DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        if not rows:
            return [], None

        postings = [_row_to_posting(row) for row in rows]
        matrix = np.vstack([
            np.frombuffer(row["embedding"], dtype=np.float32) for row in rows
        ])
        return postings, matrix

    def all_jobs(self) -> list[JobPosting]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs").fetchall()
        return [_row_to_posting(row) for row in rows]

    def stats(self) -> dict:
        with self._connect() as conn:
            by_source = {
                row["source"]: row["n"] for row in conn.execute(
                    "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source"
                )
            }
        return {
            "total": self.count(),
            "embedded": self.count(embedded_only=True),
            "by_source": by_source,
        }


def _row_to_posting(row: sqlite3.Row) -> JobPosting:
    return JobPosting(
        id=row["id"],
        source=row["source"],
        title=row["title"],
        company=row["company"],
        location=row["location"] or "",
        url=row["url"] or "",
        description=row["description"],
        posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
        remote=bool(row["remote"]),
        tags=json.loads(row["tags"]) if row["tags"] else [],
    )
