"""Brings the database up to date safely: when there are changes to apply, a
"before_migration" backup is made first. Run by the start script on every start.

    uv run python -m app.devtools.upgrade
"""

import logging
import sys
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from alembic import command
from app.db import engine
from app.services import backups

BACKEND_DIR = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)


def _config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def pending(cfg: Config) -> tuple[str | None, str]:
    """(current revision, latest revision)."""
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    return current, head


def upgrade() -> str:
    cfg = _config()
    current, head = pending(cfg)
    if current == head:
        return "The database is up to date."
    if current is not None:  # an existing database: keep a copy before changing it
        try:
            info = backups.create_backup("before_migration")
            print(f"Backed up the database first: {info.name}")
        except backups.BackupsUnsupported as exc:
            print(f"No automatic backup: {exc} Back up the database yourself before updating.")
            return "Update stopped. Back up the database, then run this again with --no-backup."
    command.upgrade(cfg, "head")
    return "Set up a new database." if current is None else "Updated the database."


def main(argv: list[str]) -> int:
    if "--no-backup" in argv:
        command.upgrade(_config(), "head")
        print("Updated the database (no backup made).")
        return 0
    print(upgrade())
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main(sys.argv[1:]))
