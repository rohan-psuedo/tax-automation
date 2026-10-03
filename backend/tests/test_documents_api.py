from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.ingestion import storage
from app.models import Document
from app.models._common import utcnow
from app.pipeline.worker import process_pending, requeue_stale
from tests import samples


def _upload(client: TestClient, company_id: int, *files: tuple[str, bytes]) -> dict:
    resp = client.post(
        f"/api/companies/{company_id}/documents",
        files=[("files", (name, data, "application/octet-stream")) for name, data in files],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run_worker() -> int:
    with SessionLocal() as db:
        return process_pending(db)


def test_upload_parse_and_view(admin_client: TestClient, company_id: int):
    result = _upload(
        admin_client,
        company_id,
        ("invoice.pdf", samples.text_pdf()),
        ("photo.jpg", samples.rotated_jpeg()),
        ("sales.xlsx", samples.xlsx_file()),
        ("virus.exe", b"MZ\x90\x00"),
        ("empty.pdf", b""),
    )
    assert [d["status"] for d in result["documents"]] == ["uploaded"] * 3
    assert result["documents"][0]["uploader_name"] == "Admin"
    assert {r["filename"]: r["reason"] for r in result["rejected"]} == {
        "virus.exe": "Unsupported file type. Upload PDF, JPG, PNG, WEBP, DOCX, XLSX or CSV.",
        "empty.pdf": "The file is empty.",
    }

    assert _run_worker() == 3
    assert _run_worker() == 0  # nothing left

    docs = admin_client.get(f"/api/companies/{company_id}/documents").json()
    assert [d["original_filename"] for d in docs] == ["sales.xlsx", "photo.jpg", "invoice.pdf"]
    assert {d["status"] for d in docs} == {"parsed"}

    pdf_id = docs[2]["id"]
    detail = admin_client.get(f"/api/documents/{pdf_id}").json()
    assert detail["page_count"] == 1 and detail["has_text_layer"] is True
    assert "SE/2026/0042" in detail["text"]
    assert detail["parsed"]["pages"][0]["number"] == 1

    page = admin_client.get(f"/api/documents/{pdf_id}/pages/1")
    assert page.status_code == 200 and page.content.startswith(b"\x89PNG")
    assert admin_client.get(f"/api/documents/{pdf_id}/pages/2").status_code == 404

    original = admin_client.get(f"/api/documents/{pdf_id}/file")
    assert original.content.startswith(b"%PDF-")
    assert "inline" in original.headers["content-disposition"]

    counts = admin_client.get(f"/api/companies/{company_id}/documents/counts").json()
    assert counts["parsed"] == 3 and counts["failed"] == 0

    sheet = admin_client.get(f"/api/documents/{docs[0]['id']}").json()
    assert sheet["parsed"]["sheets"][0]["rows"][0] == ["Invoice", "Party", "Amount"]


def test_duplicate_upload(admin_client: TestClient, company_id: int):
    pdf = samples.text_pdf()  # PDFs embed timestamps, so build the bytes once
    first = _upload(admin_client, company_id, ("a.pdf", pdf))["documents"][0]
    second = _upload(admin_client, company_id, ("copy of a.pdf", pdf))["documents"][0]
    assert second["status"] == "duplicate" and second["duplicate_of_id"] == first["id"]
    assert _run_worker() == 1  # the duplicate is not read again


def test_failed_document_and_retry(admin_client: TestClient, company_id: int):
    doc = _upload(admin_client, company_id, ("locked.pdf", samples.password_pdf()))["documents"][0]
    _run_worker()
    failed = admin_client.get(f"/api/documents/{doc['id']}").json()
    assert failed["status"] == "failed"
    assert "password-protected" in failed["error"]

    assert admin_client.post(f"/api/documents/{doc['id']}/retry").json()["status"] == "uploaded"
    assert admin_client.post(f"/api/documents/{doc['id']}/retry").status_code == 409


def test_delete_keeps_file_for_duplicates(admin_client: TestClient, company_id: int):
    pdf = samples.text_pdf()
    first = _upload(admin_client, company_id, ("a.pdf", pdf))["documents"][0]
    _run_worker()
    dup = _upload(admin_client, company_id, ("b.pdf", pdf))["documents"][0]

    assert admin_client.delete(f"/api/documents/{first['id']}").status_code == 204
    heir = admin_client.get(f"/api/documents/{dup['id']}").json()
    assert heir["status"] == "parsed" and heir["duplicate_of_id"] is None
    assert admin_client.get(f"/api/documents/{dup['id']}/pages/1").status_code == 200

    assert admin_client.delete(f"/api/documents/{dup['id']}").status_code == 204
    with SessionLocal() as db:
        assert db.query(Document).count() == 0
    events = admin_client.get("/api/audit", params={"entity_type": "document"}).json()
    assert [e["action"] for e in events][:2] == ["document.deleted", "document.deleted"]


def test_delete_removes_files(admin_client: TestClient, company_id: int):
    doc = _upload(admin_client, company_id, ("a.pdf", samples.text_pdf()))["documents"][0]
    _run_worker()
    with SessionLocal() as db:
        rel = db.get(Document, doc["id"]).stored_path
    assert storage.absolute(rel).exists() and storage.pages_dir(rel).exists()
    admin_client.delete(f"/api/documents/{doc['id']}")
    assert not storage.absolute(rel).exists() and not storage.pages_dir(rel).exists()


def test_preparer_can_only_delete_own_uploads(admin_client: TestClient, company_id: int):
    admin_doc = _upload(admin_client, company_id, ("a.pdf", samples.text_pdf()))["documents"][0]
    admin_client.post(
        "/api/users",
        json={
            "email": "p@ca.test",
            "full_name": "Prep",
            "password": "prep-pass-1",
            "role": "preparer",
        },
    )
    admin_client.cookies.clear()
    admin_client.post("/api/auth/login", json={"email": "p@ca.test", "password": "prep-pass-1"})

    assert admin_client.delete(f"/api/documents/{admin_doc['id']}").status_code == 403
    own = _upload(admin_client, company_id, ("mine.png", samples.png()))["documents"][0]
    assert admin_client.delete(f"/api/documents/{own['id']}").status_code == 204


def test_upload_requires_login(client: TestClient):
    resp = client.post("/api/companies/1/documents", files=[("files", ("a.pdf", b"%PDF-", "x"))])
    assert resp.status_code == 401


def test_stale_claims_are_requeued(admin_client: TestClient, company_id: int):
    doc = _upload(admin_client, company_id, ("a.pdf", samples.text_pdf()))["documents"][0]
    with SessionLocal() as db:
        row = db.get(Document, doc["id"])
        row.status = "parsing"
        row.claimed_at = utcnow() - timedelta(hours=1)
        db.commit()
        assert requeue_stale(db) == 1
    assert _run_worker() == 1


def test_ids_are_never_reused_after_delete(admin_client: TestClient, company_id: int):
    """The audit trail refers to documents by id, so a deleted id must not come back."""
    first = _upload(admin_client, company_id, ("a.png", samples.png(text="one")))["documents"][0]
    admin_client.delete(f"/api/documents/{first['id']}")
    second = _upload(admin_client, company_id, ("b.png", samples.png(text="two")))["documents"][0]
    assert second["id"] > first["id"]
