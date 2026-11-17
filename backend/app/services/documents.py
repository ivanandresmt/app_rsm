"""Trip PDF documents.

Guarantees:
  * The document renders a *snapshot* frozen at request time (stored in the row), so later edits never alter it, and the
    snapshot identifies the trip version + as-of time it represents.
  * Publication order: write temp file -> fsync -> atomic rename -> compute sha256 -> only then mark the row `ready`.
    A crash at any point leaves either no file or an orphan temp file, never a `ready` row without a valid file.
  * Downloads re-verify the file against the stored hash; a missing/corrupt file flips the document back to regeneration
    from the stored snapshot (self-healing), instead of serving garbage or a dead link.
"""

import hashlib
import io
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError, conflict, not_found
from app.models import Document, Trip
from app.money import fmt
from app.schemas import DocumentOut
from app.services import authz, jobs
from app.services.events import emit
from app.services.views import booking_out, load_bookings, trip_detail

log = logging.getLogger("documents")


def _now() -> datetime:
    return datetime.now(UTC)


def storage_root() -> Path:
    p = Path(get_settings().storage_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def document_out(d: Document) -> DocumentOut:
    return DocumentOut(
        id=d.id, trip_id=d.trip_id, status=d.status, trip_version=d.trip_version, as_of=d.as_of, snapshot_hash=d.snapshot_hash,  # type: ignore[arg-type]
        file_size=d.file_size, file_sha256=d.file_sha256, error=d.error, attempts=d.attempts, created_at=d.created_at, updated_at=d.updated_at,
        download_url=f"/documents/{d.id}/download" if d.status == "ready" else None,
    )


def build_snapshot(db: Session, trip: Trip, role: str) -> dict[str, Any]:
    detail = trip_detail(db, trip, role).model_dump(mode="json")
    bookings = [booking_out(b).model_dump(mode="json") for b in load_bookings(db, trip.id)]
    return {"trip": detail, "bookings": bookings}


def snapshot_hash(snap: dict[str, Any]) -> str:
    stable = {"trip": {k: v for k, v in snap["trip"].items() if k not in ("as_of", "my_role")}, "bookings": snap["bookings"]}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def request_document(db: Session, user_id: str, trip_id: str) -> tuple[Document, bool]:
    trip, role = authz.load_trip(db, trip_id, user_id, "editor")
    snap = build_snapshot(db, trip, role)
    h = snapshot_hash(snap)
    existing = db.scalar(select(Document).where(Document.trip_id == trip_id, Document.snapshot_hash == h, Document.status.in_(["queued", "running", "ready"])).order_by(Document.created_at.desc()))
    if existing is not None:
        return existing, True
    doc = Document(trip_id=trip_id, requested_by=user_id, status="queued", snapshot=snap, snapshot_hash=h, trip_version=trip.version, as_of=_now())
    db.add(doc)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        again = db.scalar(select(Document).where(Document.trip_id == trip_id, Document.snapshot_hash == h, Document.status.in_(["queued", "running"])))
        if again is None:
            raise
        return again, True
    jobs.enqueue(db, "document.generate", {"document_id": doc.id}, dedupe_key=f"doc:{doc.id}", max_attempts=get_settings().document_max_attempts)
    emit(db, trip_id, "document.updated", document_id=doc.id, status="queued")
    db.commit()
    return doc, False


def retry_document(db: Session, user_id: str, document_id: str) -> Document:
    doc = db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if doc is None:
        raise not_found("Document")
    authz.require_role(db, doc.trip_id, user_id, "editor")
    if doc.status != "failed":
        raise conflict("NOT_FAILED", f"Only failed documents can be retried (status is '{doc.status}').")
    doc.status, doc.error, doc.attempts = "queued", None, 0
    jobs.enqueue(db, "document.generate", {"document_id": doc.id}, dedupe_key=f"doc:{doc.id}", max_attempts=get_settings().document_max_attempts)
    emit(db, doc.trip_id, "document.updated", document_id=doc.id, status="queued")
    db.commit()
    return doc


# ---------------------------------------------------------------- rendering
def render_pdf(doc: Document) -> bytes:
    snap = doc.snapshot
    t = snap["trip"]
    st = getSampleStyleSheet()
    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, invariant=1, title=f"Trip: {t['name']}")
    p = lambda text, style="BodyText": Paragraph(escape(str(text)), st[style])  # noqa: E731
    el: list[Any] = [p(t["name"], "Title")]
    el.append(p(f"Destinations: {', '.join(t['destinations'])}  |  {t['starts_on']} to {t['ends_on']}  |  {t['travelers']} traveler(s)"))
    el.append(p(f"Represents trip version {doc.trip_version} as of {doc.as_of.isoformat()}  |  Snapshot {doc.snapshot_hash[:12]}"))
    el.append(p("All amounts are USD. Prices marked 'estimate' are indicative and not guaranteed."))
    el.append(Spacer(1, 6 * mm))

    el.append(p("Itinerary", "Heading2"))
    rows = [["#", "Item", "When", "Cost (USD)", "Cost status"]]
    for i in t["items"]:
        rows.append([str(i["position"] + 1), Paragraph(escape(f"{i['title']} ({i['category']})"), st["BodyText"]),
                     Paragraph(escape(f"{i['starts_at'][:16]} -> {i['ends_at'][:16]}"), st["BodyText"]), i["cost_usd"], i["cost_status"]])
    if len(rows) == 1:
        rows.append(["", "No items", "", "", ""])
    el.append(_table(rows, [10 * mm, 62 * mm, 55 * mm, 22 * mm, 25 * mm]))
    el.append(Spacer(1, 5 * mm))

    c = t["costs"]
    el.append(p("Costs", "Heading2"))
    el.append(_table([["Confirmed", c["confirmed_usd"]], ["Pending", c["pending_usd"]], ["Unresolved", c["unknown_usd"]], ["Estimated", c["estimated_usd"]],
                      ["TOTAL", c["total_usd"]], ["Budget", c["budget_usd"]], ["Remaining", c["remaining_usd"]], ["Per traveler (info)", c["per_traveler_usd"]]], [60 * mm, 40 * mm], header=False))
    el.append(Spacer(1, 5 * mm))

    el.append(p("Bookings", "Heading2"))
    rows = [["Item", "Status", "Accepted price", "Reference"]]
    for b in snap["bookings"]:
        rows.append([Paragraph(escape(b["offer_title"]), st["BodyText"]), b["status"], b["accepted_price_usd"] or f"{b['reviewed_price_usd']} (reviewed)", Paragraph(escape(b["client_reference"][:40]), st["BodyText"])])
    if len(rows) == 1:
        rows.append(["No bookings requested", "", "", ""])
    el.append(_table(rows, [62 * mm, 25 * mm, 35 * mm, 52 * mm]))

    if t["issues"]:
        el.append(Spacer(1, 5 * mm))
        el.append(p("Open issues", "Heading2"))
        for i in t["issues"]:
            el.append(p(f"[{i['severity']}] {i['message']}"))
    pdf.build(el)
    return buf.getvalue()


def _table(rows: list[list[Any]], widths: list[float], header: bool = True) -> Table:
    tb = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 8.5)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey))
    tb.setStyle(TableStyle(style))
    return tb


# ---------------------------------------------------------------- generation (worker side)
def _final_path(doc: Document) -> Path:
    return storage_root() / doc.trip_id / f"{doc.id}.pdf"


def generate(db: Session, document_id: str) -> None:
    s = get_settings()
    doc = db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if doc is None or doc.status == "ready":
        return
    doc.status = "running"
    doc.attempts += 1
    emit(db, doc.trip_id, "document.updated", document_id=doc.id, status="running")
    db.commit()

    if s.pdf_render_delay_s:
        time.sleep(s.pdf_render_delay_s)
    data = render_pdf(doc)
    final = _final_path(doc)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_suffix(".pdf.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)  # atomic publish
    sha = hashlib.sha256(data).hexdigest()

    doc = db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    assert doc is not None
    doc.status, doc.error = "ready", None
    doc.file_path = str(final.relative_to(storage_root()))
    doc.file_sha256, doc.file_size = sha, len(data)
    emit(db, doc.trip_id, "document.updated", document_id=doc.id, status="ready")
    db.commit()
    log.info("document ready %s", doc.id)


def mark_attempt_failed(db: Session, document_id: str, error: str, final: bool) -> None:
    doc = db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if doc is None or doc.status == "ready":
        return
    doc.error = error[:1000]
    doc.status = "failed" if final else "queued"
    emit(db, doc.trip_id, "document.updated", document_id=doc.id, status=doc.status)
    db.commit()


# ---------------------------------------------------------------- download + integrity
def _file_ok(doc: Document) -> bytes | None:
    if not doc.file_path:
        return None
    path = storage_root() / doc.file_path
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if hashlib.sha256(data).hexdigest() == doc.file_sha256 and len(data) == doc.file_size else None


def _requeue_from_snapshot(db: Session, doc: Document, reason: str) -> None:
    log.error("document %s inconsistent (%s); regenerating from stored snapshot", doc.id, reason)
    doc.status, doc.error, doc.attempts = "queued", f"Stored file was {reason}; regenerating from the saved snapshot.", 0
    doc.file_path = doc.file_sha256 = None
    doc.file_size = None
    jobs.enqueue(db, "document.generate", {"document_id": doc.id}, dedupe_key=f"doc:{doc.id}", max_attempts=get_settings().document_max_attempts)
    emit(db, doc.trip_id, "document.updated", document_id=doc.id, status="queued")


def read_for_download(db: Session, user_id: str, document_id: str) -> tuple[bytes, Document]:
    doc = db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if doc is None:
        raise not_found("Document")
    authz.require_role(db, doc.trip_id, user_id, "viewer")
    if doc.status != "ready":
        raise AppError(409, "DOCUMENT_NOT_READY", f"The document is '{doc.status}'.", {"status": doc.status})
    data = _file_ok(doc)
    if data is None:
        _requeue_from_snapshot(db, doc, "missing or corrupt")
        db.commit()
        raise AppError(409, "DOCUMENT_REGENERATING", "The stored file was missing or damaged; it is being regenerated from its saved snapshot. Try again shortly.", {"status": "queued"})
    return data, doc


def audit(db: Session) -> dict[str, int]:
    """Periodic consistency sweep: ready rows must have valid files; stale temp files are removed."""
    fixed = 0
    for doc in db.scalars(select(Document).where(Document.status == "ready").with_for_update(skip_locked=True)):
        if _file_ok(doc) is None:
            _requeue_from_snapshot(db, doc, "missing or corrupt")
            fixed += 1
    db.commit()
    orphans = 0
    cutoff = (_now() - timedelta(minutes=10)).timestamp()
    for tmp in storage_root().glob("*/*.tmp"):
        try:
            if tmp.stat().st_mtime < cutoff:
                tmp.unlink()
                orphans += 1
        except OSError:
            pass
    return {"repaired": fixed, "orphans_removed": orphans}


__all__ = ["fmt"]
