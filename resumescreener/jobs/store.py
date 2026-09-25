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
CREATE INDEX IF NOT EXISTS idx_jobs_company   ON jobs(company COLLATE NOCASE);

-- Company lookups: which boards a searched name resolved to, and when. Lets a
-- repeat search within the TTL answer from SQLite instead of probing six ATSs.
CREATE TABLE IF NOT EXISTS company_lookups (
    query       TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    boards      TEXT NOT NULL,
    looked_up_at TEXT NOT NULL
);
"""

SORTS = {
    "recent": "COALESCE(posted_at, fetched_at) DESC",
    "company": "company COLLATE NOCASE ASC, title COLLATE NOCASE ASC",
    "title": "title COLLATE NOCASE ASC",
}


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
                         _utc_iso(job.posted_at),
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
                     _utc_iso(job.posted_at),
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

    def replace_board(self, ats: str, slug: str, postings: list[JobPosting]) -> int:
        """
        Make the store hold exactly this board's current postings.

        Upserts what is live and deletes what is not - a role that was filled
        yesterday must not keep showing up as open. Returns how many were removed.
        """
        self.upsert_jobs(postings)
        live = {p.id for p in postings}
        prefix = f"{ats}:{slug}:"
        with self._connect() as conn:
            stored = [
                row["id"] for row in conn.execute(
                    "SELECT id FROM jobs WHERE substr(id, 1, ?) = ?", (len(prefix), prefix)
                )
            ]
            stale = [job_id for job_id in stored if job_id not in live]
            conn.executemany("DELETE FROM jobs WHERE id = ?", [(j,) for j in stale])
        return len(stale)

    def save_lookup(self, query: str, company: str, boards: list[dict]) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO company_lookups (query, company, boards, looked_up_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(query) DO UPDATE SET company=excluded.company,
                       boards=excluded.boards, looked_up_at=excluded.looked_up_at""",
                (query, company, json.dumps(boards), datetime.now(UTC).isoformat()),
            )

    def recent_lookup(self, query: str, max_age_hours: float) -> dict | None:
        """A lookup of this query made within max_age_hours, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM company_lookups WHERE query = ?", (query,)
            ).fetchone()
        if row is None:
            return None
        age = datetime.now(UTC) - datetime.fromisoformat(row["looked_up_at"])
        if age.total_seconds() > max_age_hours * 3600:
            return None
        return {"company": row["company"], "boards": json.loads(row["boards"]),
                "looked_up_at": row["looked_up_at"]}

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

    def search(
        self,
        query: str = "",
        company: str = "",
        location: str = "",
        remote_only: bool = False,
        source: str = "",
        posted_within_days: int | None = None,
        sort: str = "recent",
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[JobPosting], int]:
        """
        Browse the store. Returns (page of postings, total matching).

        Every word of `query` must appear somewhere in the posting; titles that
        contain the query rank first under the default sort, since "Python" in a
        title means more than "Python" in paragraph nine. Plain LIKE is enough at
        tens of thousands of rows; FTS5 is the step up past that.
        """
        where, params = ["1=1"], []
        for word in query.split()[:8]:
            like = f"%{_escape_like(word)}%"
            where.append(
                "(title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\' "
                "OR tags LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
            )
            params.extend([like] * 4)
        if company:
            where.append("company = ? COLLATE NOCASE")
            params.append(company)
        if location:
            where.append("location LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(location)}%")
        if remote_only:
            where.append("remote = 1")
        if source:
            where.append("source = ?")
            params.append(source)
        if posted_within_days:
            where.append("COALESCE(posted_at, fetched_at) >= ?")
            cutoff = datetime.now(UTC).timestamp() - posted_within_days * 86400
            params.append(datetime.fromtimestamp(cutoff, UTC).isoformat())

        clause = " AND ".join(where)
        order = SORTS.get(sort, SORTS["recent"])
        order_params: list = []
        if query.strip() and sort == "recent":
            order = f"(title LIKE ? ESCAPE '\\') DESC, {order}"
            order_params.append(f"%{_escape_like(query.strip())}%")

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM jobs WHERE {clause}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, *order_params, limit, offset],
            ).fetchall()
        return [_row_to_posting(row) for row in rows], total

    def board_jobs(self, ats: str, slug: str) -> list[JobPosting]:
        prefix = f"{ats}:{slug}:"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE substr(id, 1, ?) = ? "
                "ORDER BY COALESCE(posted_at, fetched_at) DESC",
                (len(prefix), prefix),
            ).fetchall()
        return [_row_to_posting(row) for row in rows]

    def get(self, job_id: str) -> JobPosting | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_posting(row) if row else None

    def companies(self, query: str = "", limit: int = 50) -> list[dict]:
        """Companies in the store with their open-role counts, largest first."""
        sql = "SELECT company, COUNT(*) AS n FROM jobs"
        params: list = []
        if query:
            sql += " WHERE company LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(query)}%")
        sql += " GROUP BY company COLLATE NOCASE ORDER BY n DESC, company LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [{"company": r["company"], "jobs": r["n"]}
                    for r in conn.execute(sql, params)]

    def stats(self) -> dict:
        with self._connect() as conn:
            by_source = {
                row["source"]: row["n"] for row in conn.execute(
                    "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source"
                )
            }
            companies = conn.execute(
                "SELECT COUNT(DISTINCT company) AS n FROM jobs"
            ).fetchone()["n"]
            remote = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE remote = 1").fetchone()["n"]
        return {
            "total": self.count(),
            "embedded": self.count(embedded_only=True),
            "companies": companies,
            "remote": remote,
            "by_source": by_source,
        }


def _utc_iso(value: datetime | None) -> str | None:
    """Store every timestamp in UTC so string comparison orders them correctly."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _escape_like(text: str) -> str:
    """Make %, _ and the escape char literal inside a LIKE pattern."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
