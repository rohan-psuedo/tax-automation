"""The background worker: reads files, asks Claude, auto-posts clean entries for companies
that chose it, and makes the daily backup."""

from fastapi.testclient import TestClient

from app.connectors.tally import TallyConnector
from app.devtools.mock_tally import DEMO_COMPANY, MockTally, httpx_transport
from app.pipeline.worker import Worker
from app.services import backups
from tests import samples
from tests.test_vouchers_api import extraction, fake_ai  # noqa: F401  (fixture)


def _worker(mock_tally: MockTally) -> Worker:
    transport = httpx_transport(mock_tally)
    return Worker(
        poll_seconds=0.01, connector_factory=lambda url: TallyConnector(url, transport=transport)
    )


def test_one_round_reads_extracts_and_auto_posts(
    admin_client: TestClient,
    company_id: int,
    mock_tally: MockTally,
    fake_ai,  # noqa: F811
):
    admin_client.post(f"/api/companies/{company_id}/ledgers/sync")
    admin_client.patch(
        f"/api/companies/{company_id}", json={"always_review": False, "auto_post": True}
    )
    admin_client.post(
        f"/api/companies/{company_id}/documents",
        files=[("files", ("inv.pdf", samples.text_pdf(), "application/pdf"))],
    )
    worker = _worker(mock_tally)
    for _ in range(4):  # read, extract, then auto-post (throttled: first round always runs)
        worker.run_once()
        worker.next_auto_post = 0.0
    assert len(mock_tally.companies[DEMO_COMPANY]["vouchers"]) == 1
    doc = admin_client.get(f"/api/companies/{company_id}/documents").json()[0]
    assert doc["voucher_status"] == "posted"


def test_no_auto_post_without_the_company_rule(
    admin_client: TestClient,
    company_id: int,
    mock_tally: MockTally,
    fake_ai,  # noqa: F811
):
    admin_client.post(f"/api/companies/{company_id}/ledgers/sync")
    admin_client.patch(f"/api/companies/{company_id}", json={"always_review": False})
    admin_client.post(
        f"/api/companies/{company_id}/documents",
        files=[("files", ("inv.pdf", samples.text_pdf(), "application/pdf"))],
    )
    worker = _worker(mock_tally)
    for _ in range(4):
        worker.run_once()
        worker.next_auto_post = 0.0
    assert mock_tally.companies[DEMO_COMPANY]["vouchers"] == []


def test_scheduled_backup_runs_from_the_worker(
    admin_client: TestClient, mock_tally: MockTally, tmp_path, monkeypatch
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "backup_dir", tmp_path)  # this test's own folder
    assert backups.list_backups() == []
    worker = _worker(mock_tally)
    worker.run_once()
    assert [b.reason for b in backups.list_backups()] == ["scheduled"]
    worker.run_once()  # not due again
    assert len(backups.list_backups()) == 1
