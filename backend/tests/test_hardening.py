"""Sign-in throttling, security headers and log files."""

import pytest
from fastapi.testclient import TestClient

from app.services import login_guard


@pytest.fixture(autouse=True)
def _fresh_guard():
    login_guard.reset()
    yield
    login_guard.reset()


def _login(client: TestClient, password: str, email: str = "admin@ca.test"):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_account_is_paused_after_five_wrong_passwords(admin_client: TestClient):
    admin_client.cookies.clear()
    for _ in range(5):
        assert _login(admin_client, "wrong-pass").status_code == 401
    blocked = _login(admin_client, "s3cret-pass")  # even the right password waits
    assert blocked.status_code == 429
    assert "15 minutes" in blocked.json()["detail"]


def test_a_successful_sign_in_clears_earlier_failures(admin_client: TestClient):
    admin_client.cookies.clear()
    for _ in range(4):
        _login(admin_client, "wrong-pass")
    assert _login(admin_client, "s3cret-pass").status_code == 200
    for _ in range(4):
        assert _login(admin_client, "wrong-pass").status_code == 401  # counting starts again


def test_pause_ends_after_the_window(admin_client: TestClient, monkeypatch):
    admin_client.cookies.clear()
    clock = [1000.0]
    monkeypatch.setattr(login_guard, "_now", lambda: clock[0])
    for _ in range(5):
        _login(admin_client, "wrong-pass")
    assert _login(admin_client, "s3cret-pass").status_code == 429
    clock[0] += login_guard.WINDOW_SECONDS + 1
    assert _login(admin_client, "s3cret-pass").status_code == 200


def test_a_forged_forwarded_address_does_not_dodge_the_pause(admin_client: TestClient):
    admin_client.cookies.clear()
    for n in range(login_guard.MAX_FAILURES_PER_ACCOUNT):
        admin_client.post(
            "/api/auth/login",
            json={"email": "admin@ca.test", "password": "wrong-pass"},
            headers={"X-Forwarded-For": f"10.0.0.{n}"},
        )
    assert _login(admin_client, "s3cret-pass").status_code == 429


def test_failed_sign_ins_are_in_the_office_activity(admin_client: TestClient):
    _login(admin_client, "wrong-pass")
    admin_client.cookies.clear()
    assert _login(admin_client, "s3cret-pass").status_code == 200
    items = admin_client.get("/api/activity").json()["items"]
    failed = next(i for i in items if i["action"] == "user.login_failed")
    assert "admin@ca.test" in failed["summary"]
    assert "wrong-pass" not in str(failed)


def test_security_headers_and_no_caching_of_private_data(admin_client: TestClient):
    resp = admin_client.get("/api/auth/me")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["cache-control"] == "no-store"


def test_logs_go_to_a_rotating_file_in_the_log_folder(client: TestClient):
    import logging

    from app.config import get_settings

    logging.getLogger("app.test").warning("hello from the test")
    for handler in logging.getLogger().handlers:
        handler.flush()
    log_file = get_settings().log_dir / "app.log"
    assert log_file.exists() and "hello from the test" in log_file.read_text(encoding="utf-8")
