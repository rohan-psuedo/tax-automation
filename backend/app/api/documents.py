from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import PathId, get_company, get_current_user
from app.ingestion import storage
from app.models import Company, Document, DocumentStatus, User, UserRole, Voucher, VoucherStatus
from app.pipeline.worker import worker
from app.schemas.api import DocumentDetail, DocumentOut, RejectedUpload, UploadResult
from app.services import documents as doc_service

router = APIRouter(prefix="/api", tags=["documents"])

MAX_FILES_PER_UPLOAD = 50


def _with_uploader_names(db: Session, docs: list[Document], schema=DocumentOut) -> list:
    """Adds uploader names and the voucher summary shown in the inbox."""
    user_ids = {d.uploaded_by for d in docs if d.uploaded_by}
    names = (
        dict(db.execute(select(User.id, User.full_name).where(User.id.in_(user_ids))).all())
        if user_ids
        else {}
    )
    doc_ids = [d.id for d in docs]
    vouchers = (
        {
            v.document_id: v
            for v in db.scalars(select(Voucher).where(Voucher.document_id.in_(doc_ids)))
        }
        if doc_ids
        else {}
    )
    out = []
    for d in docs:
        extra: dict = {"uploader_name": names.get(d.uploaded_by)}
        if v := vouchers.get(d.id):
            extra |= {
                "voucher_id": v.id,
                "voucher_status": v.status,
                "party_name": v.party_name,
                "invoice_number": v.invoice_number,
                "grand_total": float(v.grand_total) if v.grand_total is not None else None,
            }
        out.append(schema.model_validate(d).model_copy(update=extra))
    return out


def get_document(
    document_id: PathId, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Document:
    doc = db.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


@router.post("/companies/{company_id}/documents", response_model=UploadResult)
def upload_documents(
    files: list[UploadFile],
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UploadResult:
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Upload at most {MAX_FILES_PER_UPLOAD} files at a time."
        )
    limit = get_settings().max_upload_mb * 1024 * 1024
    created: list[Document] = []
    rejected: list[RejectedUpload] = []
    for upload in files:
        data = upload.file.read(limit + 1)  # one extra byte detects oversize files
        try:
            created.append(doc_service.ingest_upload(db, company, user, upload.filename, data))
        except doc_service.UploadRejected as exc:
            rejected.append(RejectedUpload(filename=upload.filename or "unnamed", reason=str(exc)))
    db.commit()
    if any(d.status == DocumentStatus.UPLOADED for d in created):
        worker.wake()
    return UploadResult(documents=_with_uploader_names(db, created), rejected=rejected)


@router.get("/companies/{company_id}/documents", response_model=list[DocumentOut])
def list_documents(
    company: Company = Depends(get_company),
    db: Session = Depends(get_db),
    status_: list[DocumentStatus] | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=200, le=1000),
) -> list[DocumentOut]:
    stmt = select(Document).where(Document.company_id == company.id)
    if status_:
        stmt = stmt.where(Document.status.in_(status_))
    if q:
        stmt = stmt.where(Document.original_filename.ilike(f"%{q}%"))
    docs = list(db.scalars(stmt.order_by(Document.id.desc()).limit(limit)))
    return _with_uploader_names(db, docs)


@router.get("/companies/{company_id}/documents/counts", response_model=dict[str, int])
def document_counts(
    company: Company = Depends(get_company), db: Session = Depends(get_db)
) -> dict[str, int]:
    rows = db.execute(
        select(Document.status, func.count(Document.id))
        .where(Document.company_id == company.id)
        .group_by(Document.status)
    ).all()
    return {s.value: 0 for s in DocumentStatus} | {row[0]: row[1] for row in rows}


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def document_detail(doc: Document = Depends(get_document), db: Session = Depends(get_db)):
    return _with_uploader_names(db, [doc], DocumentDetail)[0]


@router.get("/documents/{document_id}/file")
def document_file(doc: Document = Depends(get_document)) -> FileResponse:
    path = storage.absolute(doc.stored_path)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The stored file is missing")
    return FileResponse(
        path,
        media_type=doc.mime_type,
        filename=doc.original_filename,
        content_disposition_type="inline",
    )


@router.get("/documents/{document_id}/pages/{page}")
def document_page(page: PathId, doc: Document = Depends(get_document)) -> FileResponse:
    if page < 1:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    path = storage.page_image(doc.stored_path, page)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
    )


@router.post("/documents/{document_id}/retry", response_model=DocumentOut)
def retry(
    doc: Document = Depends(get_document),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Document:
    if doc.status != DocumentStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only documents that failed can be retried")
    doc_service.retry_document(db, doc, user.id)
    worker.wake()
    return doc


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    doc: Document = Depends(get_document),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    if user.role == UserRole.PREPARER and doc.uploaded_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only delete files you uploaded")
    if doc.status == DocumentStatus.PARSING:
        raise HTTPException(status.HTTP_409_CONFLICT, "This file is being read; try again shortly")
    voucher_status = db.scalar(select(Voucher.status).where(Voucher.document_id == doc.id))
    if voucher_status in (VoucherStatus.POSTED, VoucherStatus.POSTING):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This document's voucher is already in Tally, so the document is kept as its record.",
        )
    doc_service.delete_document(db, doc, user.id)
