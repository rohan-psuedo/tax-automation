"""Events that belong to no company (team, settings, backups) must still be visible to an
administrator, or nobody could see who changed the API key or reset a password."""

from fastapi.testclient import TestClient


def test_office_activity_shows_team_settings_and_backup_events(admin_client: TestClient):
    admin_client.post(
        "/api/users",
        json={
            "email": "r@ca.test",
            "full_name": "Ravi",
            "password": "ravi-pass-1",
            "role": "reviewer",
        },
    )
    admin_client.put("/api/settings", json={"claude_effort": "high"})
    admin_client.post("/api/backups")
    page = admin_client.get("/api/activity").json()
    actions = [i["action"] for i in page["items"]]
    for expected in ("user.created", "settings.updated", "backup.created"):
        assert expected in actions, actions
    assert all(i["company_id"] is None for i in page["items"])
    text = " ".join(i["summary"] for i in page["items"])
    assert "Ravi" in text


def test_office_activity_is_for_administrators_only(admin_client: TestClient):
    admin_client.post(
        "/api/users",
        json={
            "email": "r@ca.test",
            "full_name": "Ravi",
            "password": "ravi-pass-1",
            "role": "reviewer",
        },
    )
    admin_client.cookies.clear()
    admin_client.post("/api/auth/login", json={"email": "r@ca.test", "password": "ravi-pass-1"})
    assert admin_client.get("/api/activity").status_code == 403
