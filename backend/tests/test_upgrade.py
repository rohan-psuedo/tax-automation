"""The start script's database update: backs up before changing an existing database."""

from app.devtools import upgrade
from app.services import backups


def test_up_to_date_database_is_left_alone(monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "backup_dir", tmp_path)
    monkeypatch.setattr(upgrade, "pending", lambda cfg: ("abc", "abc"))
    monkeypatch.setattr(
        upgrade.command, "upgrade", lambda *a: (_ for _ in ()).throw(AssertionError)
    )
    assert upgrade.upgrade() == "The database is up to date."
    assert backups.list_backups() == []


def test_existing_database_is_backed_up_before_updating(monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "backup_dir", tmp_path)
    monkeypatch.setattr(upgrade, "pending", lambda cfg: ("old", "new"))
    order = []
    real_backup = backups.create_backup
    monkeypatch.setattr(
        upgrade.backups, "create_backup", lambda reason: order.append(reason) or real_backup(reason)
    )
    monkeypatch.setattr(upgrade.command, "upgrade", lambda cfg, rev: order.append(f"upgrade {rev}"))
    assert upgrade.upgrade() == "Updated the database."
    assert order == ["before_migration", "upgrade head"]
    assert [b.reason for b in backups.list_backups()] == ["before_migration"]


def test_new_database_needs_no_backup(monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "backup_dir", tmp_path)
    monkeypatch.setattr(upgrade, "pending", lambda cfg: (None, "new"))
    monkeypatch.setattr(upgrade.command, "upgrade", lambda cfg, rev: None)
    assert upgrade.upgrade() == "Set up a new database."
    assert backups.list_backups() == []
