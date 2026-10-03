"""SQLite database backups.

CONTRACT (module D):

File name: app-YYYYMMDD-HHMMSS-<reason>.db in get_settings().backup_dir, reason one of
"scheduled" | "manual" | "before_migration". Made with sqlite3's online backup API (never a
file copy: the database runs in WAL mode, so a copy can be inconsistent).

create_backup(reason: str) -> BackupInfo
    BackupInfo(name, path, size_bytes, created_at, reason)
list_backups() -> list[BackupInfo]              # newest first; ignores unrelated files
prune(keep: int | None = None) -> list[str]
    Deletes all but the newest `keep` (default settings.backup_keep); returns names deleted.
maybe_run_scheduled() -> BackupInfo | None      # "scheduled" backup if the newest backup of
                                                # any kind is older than backup_interval_hours
                                                # (or none exists), then prune(); else None
backup_path(name: str) -> Path                  # validated path for download; raises KeyError
                                                # for names not matching the pattern or missing
For a non-SQLite database_url these raise BackupsUnsupported("Use your database's own
backup tool, e.g. pg_dump.").
Uploaded files (storage_dir) are content-addressed and never changed in place; they are not
part of these backups (copy that folder with your usual file backup).

Details:
- Times in names are local wall-clock time; created_at is read back from the name.
- A backup dated more than an hour ahead of the clock was made while the clock was wrong
  (or the clock has been set back since, e.g. to work on an earlier year in Tally), so its
  real age is unknown: it never counts as recent for the schedule, and prune() deletes such
  backups before any correctly dated one.
- A backup is written to an "incomplete-*.tmp" file first and only then renamed, so a file
  with a backup name is always complete. Two backups in the same second get the next free
  second instead of replacing each other. Temporary files of a backup that was killed
  midway (service stopped, power cut) are deleted by a later backup once an hour old.
- Files in the folder that do not match the name pattern exactly, or whose date has no
  local time on this computer, are never listed, served or deleted.
- prune() always keeps at least one backup, whatever backup_keep says.
- backup_interval_hours <= 0 switches scheduled backups off.
"""

import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app import audit
from app import db as app_db
from app.config import get_settings

log = logging.getLogger(__name__)

REASONS = ("scheduled", "manual", "before_migration")
NAME_PATTERN = re.compile(r"app-(\d{8}-\d{6})-(scheduled|manual|before_migration)\.db")
PARTIAL_PATTERN = re.compile(r"incomplete-[0-9a-f]{32}\.tmp(?:-journal|-wal|-shm)?")
_STAMP = "%Y%m%d-%H%M%S"
# Room for small clock corrections (NTP, daylight saving) before a date counts as ahead.
_CLOCK_SLACK = timedelta(hours=1)
# A temporary file untouched this long belongs to a backup that was killed midway.
_STALE_PARTIAL_SECONDS = 3600
UNSUPPORTED_MESSAGE = (
    "Backups from this app work only with its built-in SQLite database. "
    "Use your database's own backup tool, e.g. pg_dump."
)

# Serialises naming, pruning and the scheduled check within this process.
_lock = threading.RLock()


class BackupsUnsupported(Exception):
    """The configured database is not a SQLite file."""


@dataclass(frozen=True)
class BackupInfo:
    name: str
    path: Path
    size_bytes: int
    created_at: datetime
    reason: str


def _now() -> datetime:
    return datetime.now().astimezone()


def _database_file() -> Path:
    url = app_db.engine.url
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise BackupsUnsupported(UNSUPPORTED_MESSAGE)
    return Path(url.database)


def _info(path: Path) -> BackupInfo | None:
    """The backup at `path`, or None for anything that is not one of our backup files."""
    match = NAME_PATTERN.fullmatch(path.name)
    if match is None or path.is_symlink() or not path.is_file():
        return None
    try:
        local = datetime.strptime(match[1], _STAMP)
        if local.strftime(_STAMP) != match[1]:
            return None
        created_at = local.astimezone()  # OSError on Windows before 1970 or after 3000
        size = path.stat().st_size
    except (ValueError, OSError, OverflowError):  # also: the file vanished meanwhile
        return None
    return BackupInfo(path.name, path, size, created_at, match[2])


def _snapshot(source: Path, target: Path) -> None:
    """Copies a consistent snapshot of the live database into `target`."""
    # A plain path, not a file: URI, which cannot name a network share (//server/share).
    if not source.is_file():  # connect() would quietly create an empty database instead
        raise FileNotFoundError(f"The database file {source} was not found.")
    with (
        closing(sqlite3.connect(source, timeout=30)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)  # one step, so it reads from a single WAL snapshot
        # The copy inherits WAL mode; switch it back so the download is one self-contained file.
        dst.execute("PRAGMA journal_mode=DELETE")


def _move_no_replace(src: Path, dst: Path) -> bool:
    try:
        if os.name == "nt":
            os.rename(src, dst)  # atomic, and refuses to replace an existing file on Windows
        else:
            os.link(src, dst)  # rename would silently replace dst on POSIX
            src.unlink()
    except FileExistsError:
        return False
    return True


def _publish(partial: Path, reason: str) -> Path:
    stamp = _now().astimezone().replace(microsecond=0)
    while True:
        final = partial.with_name(f"app-{stamp.strftime(_STAMP)}-{reason}.db")
        if not final.exists() and _move_no_replace(partial, final):
            return final
        stamp = max(stamp + timedelta(seconds=1), _now().astimezone().replace(microsecond=0))


def _remove_partial(partial: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        with suppress(OSError):
            Path(f"{partial}{suffix}").unlink(missing_ok=True)


def _remove_stale_partials(folder: Path) -> None:
    cutoff = time.time() - _STALE_PARTIAL_SECONDS  # real time: mtimes come from the real clock
    for path in folder.iterdir():
        if not PARTIAL_PATTERN.fullmatch(path.name):
            continue
        with suppress(OSError):  # e.g. still open in another process; a later backup retries
            if not path.is_symlink() and path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()


def create_backup(reason: str) -> BackupInfo:
    if reason not in REASONS:
        raise ValueError(f"Unknown backup reason {reason!r}; use one of {', '.join(REASONS)}.")
    source = _database_file()
    folder = get_settings().backup_dir
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / f"incomplete-{uuid.uuid4().hex}.tmp"
    with _lock:
        _remove_stale_partials(folder)
        try:
            _snapshot(source, partial)
            path = _publish(partial, reason)
        finally:
            _remove_partial(partial)
    info = _info(path)
    if info is None:  # only if something removed it in the meantime
        raise FileNotFoundError(f"The new backup {path.name} disappeared from {folder}.")
    log.info("Database backup %s written (%d bytes)", info.name, info.size_bytes)
    return info


def list_backups() -> list[BackupInfo]:
    _database_file()
    folder = get_settings().backup_dir
    if not folder.is_dir():
        return []
    found = [info for path in folder.iterdir() if (info := _info(path)) is not None]
    return sorted(found, key=lambda b: (b.created_at, b.name), reverse=True)


def _dated_ahead(info: BackupInfo, now: datetime) -> bool:
    return info.created_at > now + _CLOCK_SLACK


def _prune(keep: int | None, protect: str | None = None) -> list[str]:
    keep = max(get_settings().backup_keep if keep is None else keep, 1)
    deleted: list[str] = []
    with _lock:
        now = _now()
        # A stable sort of the newest-first list: `protect`, then correctly dated backups.
        ranked = sorted(
            list_backups(),
            key=lambda b: (b.name == protect, not _dated_ahead(b, now)),
            reverse=True,
        )
        for info in ranked[keep:]:
            try:
                info.path.unlink()
            except OSError:  # e.g. being downloaded right now on Windows; next run retries
                log.warning("Could not delete old backup %s", info.name, exc_info=True)
                continue
            deleted.append(info.name)
    return deleted


def prune(keep: int | None = None) -> list[str]:
    return _prune(keep)


def _is_due(backups: list[BackupInfo], interval: timedelta) -> bool:
    now = _now()
    return not any(now - b.created_at < interval and not _dated_ahead(b, now) for b in backups)


def maybe_run_scheduled() -> BackupInfo | None:
    _database_file()
    interval = timedelta(hours=get_settings().backup_interval_hours)
    if interval <= timedelta(0):
        return None
    with _lock:
        if not _is_due(list_backups(), interval):
            return None
        info = create_backup("scheduled")
        pruned = _prune(None, protect=info.name)  # never the backup it has just made
    with app_db.SessionLocal() as db:
        audit.record(
            db,
            action="backup.created",
            entity_type="backup",
            entity_id=info.name,
            data={"reason": info.reason, "size_bytes": info.size_bytes, "pruned": pruned},
        )
        db.commit()
    return info


def backup_path(name: str) -> Path:
    _database_file()
    folder = get_settings().backup_dir
    # Check the bare name first: "../x/app-...db" would otherwise pass via Path.name.
    info = _info(folder / name) if NAME_PATTERN.fullmatch(name) else None
    if info is None or info.path.resolve().parent != folder.resolve():
        raise KeyError(name)
    return info.path
