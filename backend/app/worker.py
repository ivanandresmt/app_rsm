"""Background worker: polls the Postgres jobs table. Run with `python -m app.worker`."""

import logging
import os
import signal
import socket
import time
from datetime import timedelta
from threading import Event as ThreadEvent

from app.config import get_settings
from app.db import session_factory
from app.logging_setup import new_request_id, request_id_var, setup_logging
from app.models import Job
from app.services import bookings, documents, jobs
from app.services.provider_client import get_provider

log = logging.getLogger("worker")
stop = ThreadEvent()


def handle(job: Job) -> float | None:
    """Returns None when done, or seconds until this job should run again (re-poll)."""
    db = session_factory()()
    try:
        if job.type == "document.generate":
            try:
                documents.generate(db, job.payload["document_id"])
            except Exception as e:
                db.rollback()
                final = job.attempts >= job.max_attempts
                documents.mark_attempt_failed(db, job.payload["document_id"], f"{type(e).__name__}: {e}", final)
                raise
            return None
        if job.type == "booking.reconcile":
            return bookings.reconcile(db, get_provider(), job.payload["booking_id"])
        if job.type == "maintenance.sweep":
            n = bookings.sweep_stuck(db)
            a = documents.audit(db)
            log.info("sweep: bookings_requeued=%s docs=%s", n, a)
            return float(get_settings().audit_interval_s)
        raise ValueError(f"unknown job type {job.type}")
    finally:
        db.close()


def run_once(worker_id: str) -> bool:
    s = get_settings()
    db = session_factory()()
    try:
        job = jobs.claim(db, worker_id, s.job_lease_s)
        db.commit()  # lease is now durable; others skip this job until it expires
        if job is None:
            return False
        jid, jtype = job.id, job.type
        request_id_var.set(f"job-{jid}-{new_request_id()[:6]}")
        try:
            again = handle(job)
            job = db.get_one(Job, jid)
            if again is None:
                jobs.finish(job)
            else:
                job.status, job.run_at, job.locked_by, job.lease_expires_at = "queued", jobs.now() + timedelta(seconds=again), None, None
                # polling jobs get more attempts rather than failing for being slow to converge
                job.attempts = min(job.attempts, job.max_attempts - 1)
        except Exception as e:
            db.rollback()
            job = db.get_one(Job, jid)
            will_retry = jobs.retry_or_fail(job, f"{type(e).__name__}: {e}")
            log.exception("job %s (%s) failed; retry=%s", jid, jtype, will_retry)
        db.commit()
        return True
    finally:
        db.close()


def ensure_sweep() -> None:
    db = session_factory()()
    try:
        jobs.enqueue(db, "maintenance.sweep", {}, dedupe_key="maintenance.sweep", max_attempts=1_000_000)
        db.commit()
    finally:
        db.close()


def main() -> None:
    setup_logging()
    s = get_settings()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    ensure_sweep()
    log.info("worker %s started", worker_id)
    while not stop.is_set():
        try:
            busy = run_once(worker_id)
        except Exception:
            log.exception("worker loop error")
            busy = False
        if not busy:
            stop.wait(s.job_poll_interval_s)
        else:
            time.sleep(0)
    log.info("worker %s stopped gracefully", worker_id)


if __name__ == "__main__":
    main()
