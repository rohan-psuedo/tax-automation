"""Background worker that reads uploaded documents.

Jobs live in the documents table itself (status = uploaded), so no extra queue service is
needed for a single-office install. A document is claimed with a conditional UPDATE, so
several workers can run without parsing the same file twice.

Runs inside the API process by default; to run it separately:
    uv run python -m app.pipeline.worker   (and set RUN_WORKER_IN_PROCESS=false for the API)
"""

import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import audit
from app.config import get_settings
from app.db import SessionLocal
from app.ingestion import storage
from app.models import Document, DocumentStatus
from app.models._common import utcnow
from app.parsing import ParseError, parse_file
from app.services import backups, vouchers
from app.services.connectors import ConnectorFactory

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(minutes=10)


def _claim(db: Session, doc_id: int) -> bool:
    result = db.execute(
        update(Document)
        .where(Document.id == doc_id, Document.status == DocumentStatus.UPLOADED)
        .values(
            status=DocumentStatus.PARSING,
            claimed_at=utcnow(),
            attempts=Document.attempts + 1,
        )
    )
    db.commit()
    return result.rowcount == 1


def requeue_stale(db: Session) -> int:
    """Puts back documents whose worker died mid-parse (e.g. the server was restarted)."""
    result = db.execute(
        update(Document)
        .where(
            Document.status == DocumentStatus.PARSING,
            Document.claimed_at < utcnow() - STALE_AFTER,
        )
        .values(status=DocumentStatus.UPLOADED)
    )
    db.commit()
    return result.rowcount


def process_document(db: Session, doc: Document) -> None:
    pages_out = storage.pages_dir(doc.stored_path)
    try:
        parsed = parse_file(storage.absolute(doc.stored_path), doc.kind, pages_out)
    except ParseError as exc:
        _fail(db, doc, str(exc), retryable=False)
        return
    except Exception as exc:  # unexpected: keep the worker alive, allow a retry
        log.exception("Parsing document %s failed", doc.id)
        _fail(
            db,
            doc,
            f"Unexpected error while reading the file ({exc.__class__.__name__}).",
            retryable=True,
        )
        return

    doc.status = DocumentStatus.PARSED
    doc.error = None
    vouchers.create_for_document(db, doc)
    doc.page_count = parsed.page_count
    doc.has_text_layer = parsed.has_text_layer
    doc.text = parsed.text
    doc.parsed = parsed.meta()
    doc.parsed_at = utcnow()
    audit.record(
        db,
        action="document.parsed",
        entity_type="document",
        entity_id=doc.id,
        company_id=doc.company_id,
        data={
            "pages": parsed.page_count,
            "text_layer": parsed.has_text_layer,
            "warnings": parsed.warnings,
        },
    )
    db.commit()


def _fail(db: Session, doc: Document, message: str, *, retryable: bool) -> None:
    storage.delete_files(doc.stored_path, keep_original=True)
    if retryable and doc.attempts < MAX_ATTEMPTS:
        doc.status = DocumentStatus.UPLOADED
    else:
        doc.status = DocumentStatus.FAILED
    doc.error = message
    audit.record(
        db,
        action="document.parse_failed",
        entity_type="document",
        entity_id=doc.id,
        company_id=doc.company_id,
        data={"error": message, "attempt": doc.attempts},
    )
    db.commit()


def process_pending(db: Session, limit: int = 20) -> int:
    """Reads up to `limit` queued documents. Returns how many were processed."""
    ids = list(
        db.scalars(
            select(Document.id)
            .where(Document.status == DocumentStatus.UPLOADED)
            .order_by(Document.id)
            .limit(limit)
        )
    )
    done = 0
    for doc_id in ids:
        if not _claim(db, doc_id):
            continue  # another worker got it
        doc = db.get(Document, doc_id, populate_existing=True)
        if doc is not None:
            process_document(db, doc)
            done += 1
    return done


# Auto-posting asks Tally which books are open before posting, so it runs on its own slower
# clock rather than on every poll; the backup check only needs to notice the day changing.
AUTO_POST_EVERY_SECONDS = 30.0
BACKUP_CHECK_EVERY_SECONDS = 600.0


class Worker:
    def __init__(
        self,
        poll_seconds: float | None = None,
        connector_factory: ConnectorFactory | None = None,  # tests route Tally to the mock
    ) -> None:
        self.poll_seconds = poll_seconds or get_settings().worker_poll_seconds
        self.connector_factory = connector_factory
        self.next_auto_post = 0.0  # time.monotonic() values; 0 means "due now"
        self.next_backup_check = 0.0
        self.backups_supported = True
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def wake(self) -> None:
        """Called after an upload so new files are picked up immediately."""
        self._wake.set()

    def run_forever(self) -> None:
        with SessionLocal() as db:
            requeued = requeue_stale(db) + vouchers.requeue_stale(db)
            queued = vouchers.queue_missing(db)
            if queued:
                log.info("Queued vouchers for %d document(s) read earlier", queued)
            if requeued:
                log.info("Recovered %d stale document(s)/voucher(s)", requeued)
        while not self._stop.is_set():
            if not self.run_once():
                self._wake.wait(self.poll_seconds)
                self._wake.clear()

    def run_once(self) -> bool:
        """One round of work. Returns True when there may be more to do right away. Each
        step is isolated, so a failure in one never stops the others."""
        busy = False
        try:
            with SessionLocal() as db:
                # Reading files is fast, so it goes first; then one AI extraction at a time,
                # so new uploads are never stuck behind a long batch.
                busy = process_pending(db) > 0
                busy = vouchers.process_pending(db, limit=1) > 0 or busy
        except Exception:
            log.exception("Reading documents failed")
        now = time.monotonic()
        if now >= self.next_auto_post:
            self.next_auto_post = now + AUTO_POST_EVERY_SECONDS
            try:
                with SessionLocal() as db:
                    posted = vouchers.auto_post_ready(db, self.connector_factory)
                if posted:
                    log.info(
                        "Auto-posted %d entr%s to Tally", posted, "y" if posted == 1 else "ies"
                    )
            except Exception:
                log.exception("Auto-posting failed")
        if self.backups_supported and now >= self.next_backup_check:
            self.next_backup_check = now + BACKUP_CHECK_EVERY_SECONDS
            try:
                backups.maybe_run_scheduled()
            except backups.BackupsUnsupported:
                self.backups_supported = False  # e.g. Postgres: use its own backup tool
            except Exception:
                log.exception("Scheduled backup failed")
        return busy

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="doc-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)


worker = Worker()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Document worker started")
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass
