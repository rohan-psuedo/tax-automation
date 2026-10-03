import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app import audit
from app import db as app_db
from app.config import BACKEND_DIR, get_settings
from app.db import SessionLocal, engine
from app.models import AuditEvent, User
from app.services import backups as backup_service

NOW = datetime(2026, 10, 3, 12, 0, 0).astimezone()


def _assert_temporary(folder: Path) -> None:
    folder = folder.resolve()
    assert folder.is_relative_to(Path(engine.url.database).resolve().parent)
    assert folder.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert not folder.is_relative_to(BACKEND_DIR.resolve())


@pytest.fixture(autouse=True)
def backup_dir() -> Iterator[Path]:
    folder = get_settings().backup_dir
    _assert_temporary(folder)  # this fixture deletes the folder, so be sure what it is
    if folder.exists():
        shutil.rmtree(folder)
    yield folder


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(backup_service, "_now", lambda: NOW)
    return NOW


def _name(when: datetime, reason: str = "manual") -> str:
    return f"app-{when:%Y%m%d-%H%M%S}-{reason}.db"


def _craft(folder: Path, name: str, data: bytes = b"not a real backup") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def _query(path: Path, sql: str):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchone()[0]


def _backup_events() -> list[AuditEvent]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(AuditEvent).where(AuditEvent.entity_type == "backup").order_by(AuditEvent.id)
            )
        )


def _login_as(client: TestClient, role: str) -> None:
    email = f"{role}@ca.test"
    resp = client.post(
        "/api/users",
        json={"email": email, "full_name": role.title(), "password": "role-pass-1", "role": role},
    )
    assert resp.status_code == 201, resp.text
    client.cookies.clear()
    resp = client.post("/api/auth/login", json={"email": email, "password": "role-pass-1"})
    assert resp.status_code == 200, resp.text


def test_tests_use_a_temporary_backup_folder():
    settings = get_settings()
    _assert_temporary(settings.backup_dir)
    assert not Path(engine.url.database).resolve().is_relative_to(BACKEND_DIR.resolve())


def test_backup_contains_the_current_data(admin_client: TestClient, backup_dir):
    resp = admin_client.post("/api/backups")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reason"] == "manual"
    assert backup_service.NAME_PATTERN.fullmatch(body["name"])

    path = backup_dir / body["name"]
    assert body["size_bytes"] == path.stat().st_size > 0
    assert _query(path, "SELECT email FROM users") == "admin@ca.test"
    assert _query(path, "SELECT count(*) FROM audit_events WHERE action = 'user.setup_admin'") == 1
    assert _query(path, "PRAGMA integrity_check") == "ok"
    # A single self-contained file, and nothing half-written left behind.
    assert _query(path, "PRAGMA journal_mode") == "delete"
    assert [p.name for p in backup_dir.iterdir()] == [body["name"]]

    events_in_backup = _query(path, "SELECT count(*) FROM audit_events")
    with SessionLocal() as db:
        audit.record(db, action="test.later", entity_type="test", entity_id=1)
        db.commit()
    assert _query(path, "SELECT count(*) FROM audit_events") == events_in_backup


def test_backup_is_consistent_while_the_app_writes():
    with SessionLocal() as db:
        assert db.execute(text("PRAGMA journal_mode")).scalar() == "wal"

    batch_size = 7
    stop, first_commit = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def write() -> None:
        batch = 0
        try:
            while not stop.is_set():
                with SessionLocal() as db:
                    for _ in range(batch_size):
                        audit.record(
                            db,
                            action="test.write",
                            entity_type="test",
                            entity_id=batch,
                            data={"padding": "x" * 3000},  # spread a batch over many pages
                        )
                    db.commit()
                batch += 1
                first_commit.set()
        except BaseException as exc:  # surfaced by the assertion below
            errors.append(exc)
            first_commit.set()

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert first_commit.wait(10)
        backups = [backup_service.create_backup("manual") for _ in range(5)]
    finally:
        stop.set()
        writer.join(10)
    assert not errors

    counts = [_query(b.path, "SELECT count(*) FROM audit_events") for b in backups]
    assert counts[0] > 0 and counts == sorted(counts)
    assert all(count % batch_size == 0 for count in counts), counts  # never half a batch
    assert all(_query(b.path, "PRAGMA integrity_check") == "ok" for b in backups)
    assert len({b.name for b in backups}) == 5


def test_list_is_newest_first_and_ignores_other_files(admin_client: TestClient, backup_dir):
    day = datetime(2026, 9, 1, 8, 30, 0)
    names = [
        _name(day, "scheduled"),
        _name(day + timedelta(days=1), "before_migration"),
        _name(day + timedelta(days=2, seconds=5), "manual"),
    ]
    for name in names:
        _craft(backup_dir, name)
    foreign = [
        "notes.txt",
        "app.db",
        "app-20260901-083000-weekly.db",
        "app-20261340-000000-manual.db",  # month 13
        "APP-20260901-083000-MANUAL.DB",
        "app-20260901-083000-manual.db.tmp",
        "incomplete-0123.tmp",
    ]
    for name in foreign:
        _craft(backup_dir, name)
    (backup_dir / _name(day + timedelta(days=3))).mkdir()  # a folder, not a backup

    resp = admin_client.get("/api/backups")
    assert resp.status_code == 200, resp.text
    listed = resp.json()
    assert [b["name"] for b in listed] == names[::-1]
    assert [b["reason"] for b in listed] == ["manual", "before_migration", "scheduled"]
    newest = datetime.fromisoformat(listed[0]["created_at"])
    assert newest == (day + timedelta(days=2, seconds=5)).astimezone()
    assert listed[0]["size_bytes"] == len(b"not a real backup")


def test_prune_keeps_the_newest_and_ignores_other_files(
    monkeypatch: pytest.MonkeyPatch, backup_dir
):
    start = datetime(2026, 9, 1, 0, 0, 0)
    names = [_name(start + timedelta(hours=i), "scheduled") for i in range(6)]
    for name in reversed(names):  # creation order must not matter, only the name
        _craft(backup_dir, name)
    foreign = ["keep-me.db", "app-20200101-000000-weekly.db", "incomplete-abc.tmp"]
    for name in foreign:
        _craft(backup_dir, name)

    assert sorted(backup_service.prune(keep=4)) == names[:2]
    monkeypatch.setattr(get_settings(), "backup_keep", 3)
    assert backup_service.prune() == [names[2]]
    assert backup_service.prune(keep=0) == names[3:5][::-1]  # never deletes the last one

    assert sorted(p.name for p in backup_dir.iterdir()) == sorted([names[5], *foreign])


def test_scheduled_backup_when_there_is_none(frozen_clock: datetime, backup_dir):
    info = backup_service.maybe_run_scheduled()
    assert info is not None
    assert info.name == _name(frozen_clock, "scheduled") and info.created_at == frozen_clock
    assert backup_service.maybe_run_scheduled() is None  # the new one is recent

    [event] = _backup_events()
    assert (event.action, event.entity_id, event.actor_id) == ("backup.created", info.name, None)
    assert event.data["reason"] == "scheduled" and event.data["pruned"] == []


def test_scheduled_backup_looks_at_the_newest_backup_of_any_kind(
    frozen_clock: datetime, monkeypatch: pytest.MonkeyPatch, backup_dir
):
    monkeypatch.setattr(get_settings(), "backup_interval_hours", 24.0)
    _craft(backup_dir, _name(frozen_clock - timedelta(hours=30), "scheduled"))
    recent_manual = _craft(backup_dir, _name(frozen_clock - timedelta(hours=1), "manual"))
    assert backup_service.maybe_run_scheduled() is None

    recent_manual.unlink()
    _craft(backup_dir, _name(frozen_clock - timedelta(hours=23, minutes=59), "before_migration"))
    assert backup_service.maybe_run_scheduled() is None

    monkeypatch.setattr(get_settings(), "backup_interval_hours", 23.5)
    info = backup_service.maybe_run_scheduled()
    assert info is not None and info.reason == "scheduled"
    assert backup_service.list_backups()[0].name == info.name


def test_scheduled_backup_prunes_old_ones(
    frozen_clock: datetime, monkeypatch: pytest.MonkeyPatch, backup_dir
):
    monkeypatch.setattr(get_settings(), "backup_keep", 5)
    old = [_name(frozen_clock - timedelta(days=d), "scheduled") for d in range(2, 10)]
    for name in old:
        _craft(backup_dir, name)
    _craft(backup_dir, "my-own-copy.db")

    info = backup_service.maybe_run_scheduled()
    assert info is not None
    remaining = [b.name for b in backup_service.list_backups()]
    assert remaining == [info.name, *old[:4]]
    assert (backup_dir / "my-own-copy.db").exists()
    [event] = _backup_events()
    assert sorted(event.data["pruned"]) == sorted(old[4:])


def _set_clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    monkeypatch.setattr(backup_service, "_now", lambda: when)


def test_a_backup_dated_ahead_of_the_clock_does_not_trigger_a_backup_on_every_run(
    frozen_clock: datetime, monkeypatch: pytest.MonkeyPatch, backup_dir
):
    # Made while the PC clock was a month ahead; the clock has since been corrected.
    monkeypatch.setattr(get_settings(), "backup_keep", 5)
    history = [_name(frozen_clock - timedelta(days=d), "scheduled") for d in range(1, 5)]
    ahead = _name(frozen_clock + timedelta(days=30))
    for name in [*history, ahead]:
        _craft(backup_dir, name)

    made = []
    for minute in range(8):
        _set_clock(monkeypatch, frozen_clock + timedelta(minutes=minute))
        made.append(backup_service.maybe_run_scheduled())

    assert made[0] is not None and made[1:] == [None] * 7
    assert [b.name for b in backup_service.list_backups()] == [made[0].name, *history]
    [event] = _backup_events()
    assert event.data["pruned"] == [ahead]  # its real age is unknown, so it goes first


def test_backups_dated_ahead_of_the_clock_never_push_out_current_ones(
    frozen_clock: datetime, monkeypatch: pytest.MonkeyPatch, backup_dir
):
    monkeypatch.setattr(get_settings(), "backup_keep", 3)
    ahead = [_name(frozen_clock + timedelta(days=d)) for d in (30, 31, 32)]
    for name in ahead:
        _craft(backup_dir, name)

    first = backup_service.maybe_run_scheduled()
    _set_clock(monkeypatch, frozen_clock + timedelta(hours=1))
    manual = backup_service.create_backup("manual")
    _set_clock(monkeypatch, frozen_clock + timedelta(days=2))
    second = backup_service.maybe_run_scheduled()

    assert first is not None and second is not None
    remaining = [b.name for b in backup_service.list_backups()]
    assert remaining == [second.name, manual.name, first.name]
    events = _backup_events()
    assert [e.entity_id for e in events] == [first.name, second.name]
    assert sorted(name for e in events for name in e.data["pruned"]) == sorted(ahead)


def test_scheduled_backups_while_the_clock_is_set_back(
    frozen_clock: datetime, monkeypatch: pytest.MonkeyPatch, backup_dir
):
    # Some offices move the system date back to work on an earlier year in Tally.
    monkeypatch.setattr(get_settings(), "backup_keep", 3)
    history = [_name(frozen_clock - timedelta(days=d), "scheduled") for d in (1, 2, 3)]
    for name in history:
        _craft(backup_dir, name)
    _set_clock(monkeypatch, frozen_clock - timedelta(days=365))

    info = backup_service.maybe_run_scheduled()
    assert info is not None and info.path.exists()
    assert backup_service.maybe_run_scheduled() is None
    assert {b.name for b in backup_service.list_backups()} == {info.name, *history[:2]}
    [event] = _backup_events()
    assert event.data["pruned"] == [history[2]]


def test_prune_drops_backups_dated_ahead_of_the_clock_first(frozen_clock: datetime, backup_dir):
    history = [_name(frozen_clock - timedelta(days=d)) for d in (1, 2)]
    far_ahead = _name(frozen_clock + timedelta(days=10))
    just_ahead = _name(frozen_clock + timedelta(minutes=10))  # the clock was corrected slightly
    for name in [*history, far_ahead, just_ahead]:
        _craft(backup_dir, name)

    assert backup_service.prune(keep=3) == [far_ahead]
    assert backup_service.prune(keep=1) == history
    assert [b.name for b in backup_service.list_backups()] == [just_ahead]


def test_names_with_dates_the_clock_cannot_handle_break_nothing(
    admin_client: TestClient, frozen_clock: datetime, backup_dir
):
    real = _craft(backup_dir, _name(frozen_clock - timedelta(days=2))).name
    strays = [
        "app-19691231-235959-manual.db",
        "app-19700101-000000-manual.db",
        "app-99991231-235959-scheduled.db",
    ]
    for name in strays:
        _craft(backup_dir, name)

    listed = [b.name for b in backup_service.list_backups()]
    assert real in listed
    if sys.platform == "win32":  # Windows has no local time for these dates, so they are skipped
        assert listed == [real]
    assert admin_client.get("/api/backups").status_code == 200
    for name in strays:
        assert admin_client.get(f"/api/backups/{name}").status_code in (200, 404)
    backup_service.prune(keep=1)
    assert backup_service.maybe_run_scheduled() is not None


def test_leftovers_of_an_interrupted_backup_are_cleaned_up(backup_dir):
    stale = [
        f"incomplete-{'a' * 32}.tmp",
        f"incomplete-{'a' * 32}.tmp-journal",
        f"incomplete-{'b' * 32}.tmp-wal",
    ]
    fresh = f"incomplete-{'c' * 32}.tmp"  # possibly being written by another process right now
    foreign = ["incomplete-notes.tmp", "my-copy.db"]
    two_hours_ago = time.time() - 2 * 3600
    for name in [*stale, fresh, *foreign]:
        _craft(backup_dir, name)
    for name in [*stale, *foreign]:
        os.utime(backup_dir / name, (two_hours_ago, two_hours_ago))

    info = backup_service.create_backup("manual")

    assert sorted(p.name for p in backup_dir.iterdir()) == sorted([info.name, fresh, *foreign])


def test_a_backup_gets_its_final_name_only_once_it_is_complete(
    monkeypatch: pytest.MonkeyPatch, backup_dir
):
    real_snapshot = backup_service._snapshot
    written_to: list[Path] = []
    in_folder_when_written: list[str] = []

    def snapshot(source: Path, target: Path) -> None:
        real_snapshot(source, target)
        written_to.append(target)
        in_folder_when_written.extend(p.name for p in backup_dir.iterdir())

    monkeypatch.setattr(backup_service, "_snapshot", snapshot)
    info = backup_service.create_backup("manual")

    [target] = written_to
    assert target.parent == backup_dir
    assert re.fullmatch(r"incomplete-[0-9a-f]{32}\.tmp", target.name)
    assert in_folder_when_written == [target.name]  # nothing looked like a backup yet
    assert [p.name for p in backup_dir.iterdir()] == [info.name]
    assert _query(info.path, "PRAGMA integrity_check") == "ok"


def test_a_failed_backup_leaves_nothing_behind(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch, backup_dir
):
    def broken_snapshot(source: Path, target: Path) -> None:
        target.write_bytes(b"SQLite format 3\x00" + b"half written" * 100)
        Path(f"{target}-journal").write_bytes(b"journal")
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backup_service, "_snapshot", broken_snapshot)

    resp = admin_client.post("/api/backups")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail.startswith("The backup could not be written (database or disk is full).")
    assert "free space" in detail
    assert list(backup_dir.iterdir()) == []

    with pytest.raises(sqlite3.OperationalError):
        backup_service.maybe_run_scheduled()
    assert list(backup_dir.iterdir()) == []
    assert _backup_events() == []


def test_a_missing_database_is_reported_and_not_replaced_by_an_empty_one(
    monkeypatch: pytest.MonkeyPatch, backup_dir
):
    missing = Path(engine.url.database).resolve().with_name("missing.db")
    monkeypatch.setattr(
        app_db, "engine", SimpleNamespace(url=make_url(f"sqlite:///{missing.as_posix()}"))
    )
    with pytest.raises(FileNotFoundError):
        backup_service.create_backup("manual")
    assert not missing.exists()
    assert not backup_dir.exists() or not any(backup_dir.iterdir())


def test_the_database_is_opened_by_its_plain_path(monkeypatch: pytest.MonkeyPatch, backup_dir):
    # A file: URI cannot name a database on a network share, which the app itself opens fine.
    with pytest.raises(sqlite3.OperationalError, match="invalid uri authority"):
        sqlite3.connect("file://fileserver/share/app.db?mode=rw", uri=True)

    real_connect = sqlite3.connect
    opened: list[tuple[object, bool]] = []

    def connect(database, *args, **kwargs):
        opened.append((database, kwargs.get("uri", False)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    backup_service.create_backup("manual")

    assert opened[0] == (Path(engine.url.database), False)


def test_scheduled_backups_can_be_switched_off(
    frozen_clock: datetime, monkeypatch: pytest.MonkeyPatch, backup_dir
):
    monkeypatch.setattr(get_settings(), "backup_interval_hours", 0)
    assert backup_service.maybe_run_scheduled() is None
    assert not backup_dir.exists() or not any(backup_dir.iterdir())


def test_backups_in_the_same_second_get_unique_names(frozen_clock: datetime, backup_dir):
    taken = _craft(backup_dir, _name(frozen_clock), b"someone else's file")

    made = [backup_service.create_backup("manual") for _ in range(3)]
    other = backup_service.create_backup("before_migration")

    assert [b.name for b in made] == [_name(frozen_clock + timedelta(seconds=s)) for s in (1, 2, 3)]
    assert other.name == _name(frozen_clock, "before_migration")
    assert taken.read_bytes() == b"someone else's file"  # never replaced
    assert all(_query(b.path, "PRAGMA integrity_check") == "ok" for b in [*made, other])
    assert not list(backup_dir.glob("incomplete-*"))


def test_unknown_reason_is_refused():
    with pytest.raises(ValueError):
        backup_service.create_backup("weekly")


def test_only_admins_can_use_backups(client: TestClient, admin_client: TestClient):
    name = admin_client.post("/api/backups").json()["name"]
    for role in ("reviewer", "preparer"):
        _login_as(admin_client, role)
        assert admin_client.get("/api/backups").status_code == 403
        assert admin_client.post("/api/backups").status_code == 403
        assert admin_client.get(f"/api/backups/{name}").status_code == 403
        admin_client.cookies.clear()
        admin_client.post(
            "/api/auth/login", json={"email": "admin@ca.test", "password": "s3cret-pass"}
        )

    client.cookies.clear()
    assert client.get("/api/backups").status_code == 401
    assert client.post("/api/backups").status_code == 401
    assert client.get(f"/api/backups/{name}").status_code == 401


def test_download_and_audit(admin_client: TestClient, backup_dir):
    created = admin_client.post("/api/backups").json()

    resp = admin_client.get(f"/api/backups/{created['name']}")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == f'attachment; filename="{created["name"]}"'
    assert resp.headers["content-type"] == "application/vnd.sqlite3"
    assert resp.content == (backup_dir / created["name"]).read_bytes()
    assert resp.content.startswith(b"SQLite format 3\x00")

    with SessionLocal() as db:
        admin_id = db.scalar(select(User.id).where(User.email == "admin@ca.test"))
    events = [(e.action, e.entity_id, e.actor_id) for e in _backup_events()]
    assert events == [
        ("backup.created", created["name"], admin_id),
        ("backup.downloaded", created["name"], admin_id),
    ]


def test_download_refuses_anything_but_a_backup_name(admin_client: TestClient, backup_dir):
    real = admin_client.post("/api/backups").json()["name"]
    _craft(backup_dir, "app.db")
    _craft(backup_dir, real.replace(".db", ".sqlite"))
    (backup_dir / _name(datetime(2026, 1, 1))).mkdir()
    attempts = [
        "../test.db",  # the live test database next to the backup folder
        "..%2Ftest.db",
        "%2E%2E%2Ftest.db",
        "..%5Ctest.db",
        "..%5C..%5Ctest.db",
        "app.db",
        real.replace(".db", ".sqlite"),
        f"{real}%0A",
        f"{real}%00",
        f"{real}.",
        real.upper(),
        f"..%2Fbackups%2F{real}",
        _name(datetime(2026, 1, 2)),  # well formed but missing
        _name(datetime(2026, 1, 1)),  # well formed but a folder
        "app-20261340-000000-manual.db",
    ]
    for attempt in attempts:
        resp = admin_client.get(f"/api/backups/{attempt}")
        assert resp.status_code == 404, (attempt, resp.status_code)

    for name in ["../test.db", "..\\test.db", f"../backups/{real}", f"{real}\n", "app.db"]:
        with pytest.raises(KeyError):
            backup_service.backup_path(name)
    assert backup_service.backup_path(real) == backup_dir / real
    assert [e.action for e in _backup_events()] == ["backup.created"]  # no download logged


def test_other_databases_are_not_supported(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        app_db, "engine", SimpleNamespace(url=make_url("postgresql://tax:pw@db/tax"))
    )
    for resp in (admin_client.get("/api/backups"), admin_client.post("/api/backups")):
        assert resp.status_code == 409
        assert "pg_dump" in resp.json()["detail"]
    assert admin_client.get(f"/api/backups/{_name(NOW)}").status_code == 409

    for call in (backup_service.list_backups, backup_service.maybe_run_scheduled):
        with pytest.raises(backup_service.BackupsUnsupported):
            call()

    monkeypatch.setattr(app_db, "engine", SimpleNamespace(url=make_url("sqlite://")))
    with pytest.raises(backup_service.BackupsUnsupported):
        backup_service.create_backup("manual")
