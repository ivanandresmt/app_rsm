"""Durable job queue on Postgres. Claiming uses FOR UPDATE SKIP LOCKED; crashed workers are recovered by lease expiry."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Job


def now() -> datetime:
    return datetime.now(UTC)


def enqueue(
    db: Session,
    type_: str,
    payload: dict[str, Any],
    *,
    run_at: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> None:
    """Idempotent when dedupe_key is set: an already queued/running job with that key is kept."""
    stmt = pg_insert(Job).values(
        type=type_, payload=payload, run_at=run_at or now(), dedupe_key=dedupe_key, max_attempts=max_attempts, status="queued", attempts=0
    )
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=text("status IN ('queued','running') AND dedupe_key IS NOT NULL"))
    db.execute(stmt)


def claim(db: Session, worker_id: str, lease_s: int) -> Job | None:
    t = now()
    job = db.scalar(
        select(Job)
        .where(
            or_(
                and_(Job.status == "queued", Job.run_at <= t),
                and_(Job.status == "running", Job.lease_expires_at < t),  # worker died mid-job
            )
        )
        .order_by(Job.run_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = "running"
    job.attempts += 1
    job.locked_by = worker_id
    job.lease_expires_at = t + timedelta(seconds=lease_s)
    db.flush()
    return job


def finish(job: Job) -> None:
    job.status = "done"
    job.locked_by = None
    job.lease_expires_at = None


def retry_or_fail(job: Job, error: str, backoff_s: float | None = None) -> bool:
    """Returns True if the job will run again."""
    job.last_error = error[:2000]
    job.locked_by = None
    job.lease_expires_at = None
    if job.attempts >= job.max_attempts:
        job.status = "failed"
        return False
    delay = backoff_s if backoff_s is not None else min(2 ** job.attempts, 60)
    job.status = "queued"
    job.run_at = now() + timedelta(seconds=delay)
    return True
