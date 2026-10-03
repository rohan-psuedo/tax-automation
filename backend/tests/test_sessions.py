"""A password reset, a password change or a deactivation must end the person's existing
sessions; otherwise someone who learned the old password (or kept an open browser) stays in."""

from fastapi.testclient import TestClient

from app.main import app
from app.security import COOKIE_NAME


def _add_user(admin: TestClient, email: str = "p@ca.test") -> int:
    resp = admin.post(
        "/api/users",
        json={"email": email, "full_name": "Prep", "password": "first-pass-1", "role": "preparer"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _session(email: str, password: str) -> TestClient:
    other = TestClient(app)
    assert (
        other.post("/api/auth/login", json={"email": email, "password": password}).status_code
        == 200
    )
    return other


def test_admin_password_reset_ends_existing_sessions(admin_client: TestClient):
    uid = _add_user(admin_client)
    stolen = _session("p@ca.test", "first-pass-1")
    assert stolen.get("/api/auth/me").status_code == 200

    reset = admin_client.post(f"/api/users/{uid}/password", json={"password": "second-pass-2"})
    assert reset.status_code == 204
    assert stolen.get("/api/auth/me").status_code == 401
    assert _session("p@ca.test", "second-pass-2").get("/api/auth/me").status_code == 200


def test_own_password_change_keeps_this_session_and_ends_others(admin_client: TestClient):
    _add_user(admin_client)
    laptop = _session("p@ca.test", "first-pass-1")
    phone = _session("p@ca.test", "first-pass-1")

    changed = laptop.post(
        "/api/auth/password",
        json={"current_password": "first-pass-1", "new_password": "second-pass-2"},
    )
    assert changed.status_code == 204
    assert laptop.get("/api/auth/me").status_code == 200  # got a fresh session
    assert phone.get("/api/auth/me").status_code == 401


def test_reactivating_a_user_does_not_revive_old_sessions(admin_client: TestClient):
    uid = _add_user(admin_client)
    old = _session("p@ca.test", "first-pass-1")
    old_cookie = old.cookies.get(COOKIE_NAME)
    admin_client.patch(f"/api/users/{uid}", json={"is_active": False})
    admin_client.patch(f"/api/users/{uid}", json={"is_active": True})
    old.cookies.set(COOKIE_NAME, old_cookie)
    assert old.get("/api/auth/me").status_code == 401
