"""never reuse ids on sqlite

Without AUTOINCREMENT, SQLite hands out the id of the most recently deleted row again,
so the audit trail could mix events for two different documents under one id.
Postgres sequences never reuse ids, so this only applies to SQLite.

Revision ID: 41534f2133cd
Revises: 4b13a5d2f480
Create Date: 2026-10-02 20:04:50.444792

"""

from collections.abc import Sequence

from alembic import op

revision: str = "41534f2133cd"
down_revision: str | Sequence[str] | None = "4b13a5d2f480"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ["users", "companies", "documents", "posting_attempts", "audit_events"]


def _rebuild(autoincrement: bool) -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for table in TABLES:
        with op.batch_alter_table(
            table, recreate="always", table_kwargs={"sqlite_autoincrement": autoincrement}
        ):
            pass


def upgrade() -> None:
    _rebuild(True)


def downgrade() -> None:
    _rebuild(False)
