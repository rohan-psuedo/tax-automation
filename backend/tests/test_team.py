"""Team management: editing users, resetting and changing passwords, and lockout of
deactivated users."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import auth
from app.db import SessionLocal
from app.main import app
from app.models import AuditEvent, User

ADMIN = {"email": "admin@ca.test", "password": "s3cret-pass"}  # as in conftest
WRONG_LOGIN = "The email or password is not correct. Check both and try again."
TURNED_OFF = "Your account has been turned off. Ask an admin in your office to turn it back on."


def _create_user(client: TestClient, email: str, role: str = "preparer", password="pass-1234"):
    resp = client.post(
        "/api/users",
        json={"email": email, "full_name": email.split("@")[0], "password": password, "role": role},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login(email: str, password: str) -> TestClient:
    """A separate browser session, so the admin's stays logged in."""
    session = TestClient(app)
    resp = session.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return session


def _events(action: str) -> list[AuditEvent]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)
            )
        )


def _all_audit_data() -> str:
    with SessionLocal() as db:
        return json.dumps([e.data for e in db.scalars(select(AuditEvent))])


def _admin_id(client: TestClient) -> int:
    return client.get("/api/auth/me").json()["id"]


# -- editing users ----------------------------------------------------------------------


def test_admin_edits_name_and_role(admin_client: TestClient):
    user = _create_user(admin_client, "priya@ca.test")
    resp = admin_client.patch(
        f"/api/users/{user['id']}", json={"full_name": "Priya Shah", "role": "reviewer"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() | {"id": 0} == {
        "id": 0,
        "email": "priya@ca.test",
        "full_name": "Priya Shah",
        "role": "reviewer",
        "is_active": True,
    }

    [event] = _events("user.updated")
    assert event.entity_id == str(user["id"]) and event.actor_id == _admin_id(admin_client)
    assert event.data == {
        "before": {"full_name": "priya", "role": "preparer"},
        "after": {"full_name": "Priya Shah", "role": "reviewer"},
    }


def test_only_changed_fields_are_audited(admin_client: TestClient):
    user = _create_user(admin_client, "ravi@ca.test", role="reviewer")
    same = admin_client.patch(
        f"/api/users/{user['id']}", json={"role": "reviewer", "full_name": None}
    )
    assert same.status_code == 200 and same.json()["full_name"] == "ravi"
    assert _events("user.updated") == []

    admin_client.patch(f"/api/users/{user['id']}", json={"role": "reviewer", "is_active": False})
    [event] = _events("user.updated")
    assert event.data == {"before": {"is_active": True}, "after": {"is_active": False}}


def test_unknown_user(admin_client: TestClient):
    assert admin_client.patch("/api/users/999", json={"role": "admin"}).status_code == 404
    reset = admin_client.post("/api/users/999/password", json={"password": "new-pass-123"})
    assert reset.status_code == 404


def test_admin_cannot_deactivate_themselves(admin_client: TestClient):
    _create_user(admin_client, "second@ca.test", role="admin")  # not the last admin either
    resp = admin_client.patch(f"/api/users/{_admin_id(admin_client)}", json={"is_active": False})
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "You can't deactivate your own account. Ask another admin to do it."
    )
    assert admin_client.get("/api/auth/me").status_code == 200
    assert _events("user.updated") == []


def test_last_active_admin_cannot_be_demoted(admin_client: TestClient):
    me = _admin_id(admin_client)
    resp = admin_client.patch(f"/api/users/{me}", json={"role": "reviewer"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "This is the only active admin. Make another user an admin first, then change this one."
    )

    # An inactive admin doesn't count: they can't sign in to manage the office.
    other = _create_user(admin_client, "second@ca.test", role="admin")
    assert (
        admin_client.patch(f"/api/users/{other['id']}", json={"is_active": False}).status_code
        == 200
    )
    assert admin_client.patch(f"/api/users/{me}", json={"role": "reviewer"}).status_code == 409

    # With another active admin, stepping down is fine, and then admin pages are closed.
    admin_client.patch(f"/api/users/{other['id']}", json={"is_active": True})
    stepped_down = admin_client.patch(f"/api/users/{me}", json={"role": "reviewer"})
    assert stepped_down.status_code == 200 and stepped_down.json()["role"] == "reviewer"
    assert admin_client.get("/api/users").status_code == 403


def test_two_admins_stepping_down_at_once_leave_one_admin(admin_client: TestClient, monkeypatch):
    _create_user(admin_client, "second@ca.test", role="admin", password="second-pass-1")
    second = _login("second@ca.test", "second-pass-1")
    both_checked = threading.Barrier(2, timeout=10)
    check = auth._check_update_allowed

    def check_then_wait(*args):
        check(*args)
        both_checked.wait()  # as when both requests arrive at the same moment

    monkeypatch.setattr(auth, "_check_update_allowed", check_then_wait)

    def step_down(session: TestClient) -> int:
        me = session.get("/api/auth/me").json()["id"]
        return session.patch(f"/api/users/{me}", json={"role": "reviewer"}).status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(step_down, [admin_client, second])) == [200, 409]
    with SessionLocal() as db:
        admins = db.scalars(select(User).where(User.role == "admin", User.is_active.is_(True)))
        assert len(list(admins)) == 1
    assert len(_events("user.updated")) == 1


def test_the_last_admin_can_still_be_renamed(admin_client: TestClient):
    me = _admin_id(admin_client)
    resp = admin_client.patch(f"/api/users/{me}", json={"full_name": "Office Admin"})
    assert resp.status_code == 200 and resp.json()["full_name"] == "Office Admin"


@pytest.mark.parametrize("role", ["reviewer", "preparer"])
def test_only_admins_manage_the_team(admin_client: TestClient, role: str):
    target = _create_user(admin_client, "target@ca.test")
    _create_user(admin_client, "member@ca.test", role=role, password="member-pass-1")
    member = _login("member@ca.test", "member-pass-1")

    assert member.get("/api/users").status_code == 403
    assert member.patch(f"/api/users/{target['id']}", json={"role": "admin"}).status_code == 403
    reset = member.post(f"/api/users/{target['id']}/password", json={"password": "hijack-123"})
    assert reset.status_code == 403
    assert _events("user.updated") == [] and _events("user.password_reset") == []


def test_team_endpoints_need_a_session(client: TestClient):
    assert client.patch("/api/users/1", json={"role": "admin"}).status_code == 401
    assert client.post("/api/users/1/password", json={"password": "x" * 10}).status_code == 401
    body = {"current_password": "whatever", "new_password": "new-pass-123"}
    assert client.post("/api/auth/password", json=body).status_code == 401


# -- passwords ------------------------------------------------------------------------


def test_admin_resets_a_password(admin_client: TestClient):
    user = _create_user(admin_client, "forgot@ca.test", password="old-pass-123")
    resp = admin_client.post(f"/api/users/{user['id']}/password", json={"password": "new-pass-456"})
    assert resp.status_code == 204 and resp.content == b""

    old = TestClient(app).post(
        "/api/auth/login", json={"email": "forgot@ca.test", "password": "old-pass-123"}
    )
    assert old.status_code == 401 and old.json()["detail"] == WRONG_LOGIN
    assert _login("forgot@ca.test", "new-pass-456").get("/api/auth/me").status_code == 200

    [event] = _events("user.password_reset")
    assert event.entity_id == str(user["id"]) and event.actor_id == _admin_id(admin_client)
    data = _all_audit_data()
    assert "new-pass-456" not in data and "old-pass-123" not in data


def test_reset_password_rules(admin_client: TestClient):
    user = _create_user(admin_client, "short@ca.test")
    resp = admin_client.post(f"/api/users/{user['id']}/password", json={"password": "short"})
    assert resp.status_code == 422
    assert _events("user.password_reset") == []


def test_user_changes_own_password(admin_client: TestClient):
    _create_user(admin_client, "me@ca.test", password="first-pass-1")
    me = _login("me@ca.test", "first-pass-1")

    wrong = me.post(
        "/api/auth/password",
        json={"current_password": "not-my-pass", "new_password": "second-pass-2"},
    )
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == (
        "Your current password is not correct. Type it again to change your password."
    )
    assert _events("user.password_changed") == []

    too_short = me.post(
        "/api/auth/password", json={"current_password": "first-pass-1", "new_password": "short"}
    )
    assert too_short.status_code == 422

    ok = me.post(
        "/api/auth/password",
        json={"current_password": "first-pass-1", "new_password": "second-pass-2"},
    )
    assert ok.status_code == 204
    assert _login("me@ca.test", "second-pass-2").get("/api/auth/me").status_code == 200
    stale = TestClient(app).post(
        "/api/auth/login", json={"email": "me@ca.test", "password": "first-pass-1"}
    )
    assert stale.status_code == 401

    [event] = _events("user.password_changed")
    user_id = me.get("/api/auth/me").json()["id"]
    assert event.entity_id == str(user_id) and event.actor_id == user_id
    data = _all_audit_data()
    assert "first-pass-1" not in data and "second-pass-2" not in data


def test_admin_changes_own_password(admin_client: TestClient):
    resp = admin_client.post(
        "/api/auth/password",
        json={"current_password": ADMIN["password"], "new_password": "brand-new-pass"},
    )
    assert resp.status_code == 204
    assert _login(ADMIN["email"], "brand-new-pass").get("/api/users").status_code == 200


# -- deactivated users ----------------------------------------------------------------


def test_deactivated_user_is_locked_out(admin_client: TestClient):
    user = _create_user(admin_client, "leaver@ca.test", role="reviewer", password="leaver-pass")
    session = _login("leaver@ca.test", "leaver-pass")
    assert session.get("/api/auth/me").status_code == 200

    off = admin_client.patch(f"/api/users/{user['id']}", json={"is_active": False})
    assert off.status_code == 200 and off.json()["is_active"] is False

    # The session they already had stops working at once...
    assert session.get("/api/auth/me").status_code == 401
    assert session.get("/api/companies").status_code == 401
    body = {"current_password": "leaver-pass", "new_password": "sneaky-pass-1"}
    assert session.post("/api/auth/password", json=body).status_code == 401
    # ...and they can't sign in again. With the right password they are told why, so they
    # ask for the account to be turned on rather than for a new password.
    again = TestClient(app).post(
        "/api/auth/login", json={"email": "leaver@ca.test", "password": "leaver-pass"}
    )
    assert again.status_code == 403 and again.json()["detail"] == TURNED_OFF
    assert "ta_session" not in again.cookies
    # A wrong password gets the usual answer, so nothing is given away to someone guessing.
    guess = TestClient(app).post(
        "/api/auth/login", json={"email": "leaver@ca.test", "password": "a-good-guess"}
    )
    assert guess.status_code == 401 and guess.json()["detail"] == WRONG_LOGIN
    assert [e.entity_id for e in _events("user.login")].count(str(user["id"])) == 1

    # Reactivated, the same password works again.
    admin_client.patch(f"/api/users/{user['id']}", json={"is_active": True})
    assert _login("leaver@ca.test", "leaver-pass").get("/api/auth/me").status_code == 200
    assert [e.data["after"] for e in _events("user.updated")] == [
        {"is_active": False},
        {"is_active": True},
    ]


def test_unknown_email_gets_the_same_answer_as_a_wrong_password(client: TestClient):
    resp = client.post("/api/auth/login", json={"email": "nobody@ca.test", "password": "x" * 10})
    assert resp.status_code == 401 and resp.json()["detail"] == WRONG_LOGIN
