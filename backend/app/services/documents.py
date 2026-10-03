from pathlib import PurePath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.config import get_settings
from app.ingestion import filetypes, storage
from app.models import Company, Document, DocumentStatus, User


class UploadRejected(ValueError):
    pass


def _clean_filename(name: str | None) -> str:
    base = PurePath((name or "").replace("\\", "/")).name.strip()
    return base[:255] or "unnamed"


def ingest_upload(
    db: Session, company: Company, user: User, filename: str | None, data: bytes
) -> Document:
    """Stores one uploaded file and queues it for reading. Identical files already uploaded
    for this company are recorded as duplicates and not read again."""
    name = _clean_filename(filename)
    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    if not data:
        raise UploadRejected("The file is empty.")
    if len(data) > max_bytes:
        raise UploadRejected(f"The file is larger than {get_settings().max_upload_mb} MB.")
    try:
        ftype = filetypes.detect(name, data)
    except filetypes.UnsupportedFile as exc:
        raise UploadRejected(str(exc)) from exc

    sha = storage.sha256_of(data)
    original = db.scalar(
        select(Document)
        .where(
            Document.company_id == company.id,
            Document.sha256 == sha,
            Document.status != DocumentStatus.DUPLICATE,
        )
        .order_by(Document.id)
        .limit(1)
    )
    rel_path = (
        original.stored_path
        if original
        else storage.save_original(company.id, sha, ftype.ext, data)
    )
    doc = Document(
        company_id=company.id,
        uploaded_by=user.id,
        original_filename=name,
        stored_path=rel_path,
        sha256=sha,
        mime_type=ftype.mime,
        size_bytes=len(data),
        kind=ftype.kind,
        status=DocumentStatus.DUPLICATE if original else DocumentStatus.UPLOADED,
        duplicate_of_id=original.id if original else None,
    )
    db.add(doc)
    db.flush()
    audit.record(
        db,
        action="document.duplicate" if original else "document.uploaded",
        entity_type="document",
        entity_id=doc.id,
        company_id=company.id,
        actor_id=user.id,
        data={
            "filename": name,
            "sha256": sha,
            "size": len(data),
            "duplicate_of": doc.duplicate_of_id,
        },
    )
    return doc


def delete_document(db: Session, doc: Document, actor_id: int) -> None:
    others = db.scalar(
        select(func.count(Document.id)).where(
            Document.stored_path == doc.stored_path, Document.id != doc.id
        )
    )
    # Duplicates pointing at this document lose their original; make the oldest of them
    # the new original so the file and its parsed pages stay available.
    dependants = list(
        db.scalars(select(Document).where(Document.duplicate_of_id == doc.id).order_by(Document.id))
    )
    if dependants:
        heir, rest = dependants[0], dependants[1:]
        heir.duplicate_of_id = None
        heir.status = (
            doc.status if doc.status != DocumentStatus.PARSING else DocumentStatus.UPLOADED
        )
        for field in ("page_count", "has_text_layer", "text", "parsed", "parsed_at", "error"):
            setattr(heir, field, getattr(doc, field))
        for d in rest:
            d.duplicate_of_id = heir.id

    audit.record(
        db,
        action="document.deleted",
        entity_type="document",
        entity_id=doc.id,
        company_id=doc.company_id,
        actor_id=actor_id,
        data={"filename": doc.original_filename, "sha256": doc.sha256},
    )
    db.delete(doc)
    db.commit()
    if not others:
        storage.delete_files(doc.stored_path)


def retry_document(db: Session, doc: Document, actor_id: int) -> Document:
    doc.status = DocumentStatus.UPLOADED
    doc.error = None
    audit.record(
        db,
        action="document.retry",
        entity_type="document",
        entity_id=doc.id,
        company_id=doc.company_id,
        actor_id=actor_id,
    )
    db.commit()
    return doc
