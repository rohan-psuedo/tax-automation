"""Times are stored in UTC. SQLite keeps no timezone, so every timestamp read back must be
marked as UTC; otherwise browsers show it as local time (5 h 30 min off in India)."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tests import samples


def _is_utc(value: str) -> bool:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def test_api_timestamps_carry_utc(admin_client: TestClient, company_id: int):
    before = datetime.now(UTC)
    upload = admin_client.post(
        f"/api/companies/{company_id}/documents",
        files=[("files", ("a.pdf", samples.text_pdf(), "application/pdf"))],
    ).json()["documents"][0]
    detail = admin_client.get(f"/api/documents/{upload['id']}").json()
    company = admin_client.get(f"/api/companies/{company_id}").json()
    events = admin_client.get("/api/audit").json()

    for value in (
        upload["created_at"],
        detail["created_at"],
        company["created_at"],
        events[0]["created_at"],
    ):
        assert _is_utc(value), value
    created = datetime.fromisoformat(detail["created_at"].replace("Z", "+00:00"))
    assert abs(created - before) < timedelta(minutes=1)  # the right instant, not shifted
