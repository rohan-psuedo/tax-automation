"""Database backups (administrators only).

CONTRACT (module D):

GET  /api/backups              -> list[BackupOut], newest first
POST /api/backups              -> BackupOut (201), a "manual" backup made now
GET  /api/backups/{name}       -> the backup file as a download
    name must match the backup naming pattern exactly (no path traversal); 404 otherwise.
Every action is audited ("backup.created", "backup.downloaded").
A database that is not SQLite answers 409 with what to use instead.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.deps import require_role
from app.models import User
from app.schemas.api import BackupOut
from app.services import backups as backup_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backups", tags=["backups"])


@contextmanager
def _supported() -> Iterator[None]:
    try:
        yield
    except backup_service.BackupsUnsupported as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


def _out(info: backup_service.BackupInfo) -> BackupOut:
    return BackupOut(
        name=info.name, size_bytes=info.size_bytes, created_at=info.created_at, reason=info.reason
    )


@router.get("", response_model=list[BackupOut])
def list_backups(_: User = Depends(require_role())) -> list[BackupOut]:
    with _supported():
        return [_out(info) for info in backup_service.list_backups()]


@router.post("", response_model=BackupOut, status_code=status.HTTP_201_CREATED)
def create_backup(
    db: Session = Depends(get_db), admin: User = Depends(require_role())
) -> BackupOut:
    with _supported():
        try:
            info = backup_service.create_backup("manual")
        except (OSError, sqlite3.Error) as exc:
            log.exception("Manual backup failed")
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"The backup could not be written ({exc}). Check that the backup folder exists, "
                "is writable and has free space, then try again.",
            ) from exc
    audit.record(
        db,
        action="backup.created",
        entity_type="backup",
        entity_id=info.name,
        actor_id=admin.id,
        data={"reason": info.reason, "size_bytes": info.size_bytes},
    )
    db.commit()
    return _out(info)


@router.get("/{name}")
def download_backup(
    name: str, db: Session = Depends(get_db), admin: User = Depends(require_role())
) -> FileResponse:
    with _supported():
        try:
            path = backup_service.backup_path(name)
        except KeyError:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "This backup does not exist. Pick one from the list of backups.",
            ) from None
    audit.record(
        db,
        action="backup.downloaded",
        entity_type="backup",
        entity_id=name,
        actor_id=admin.id,
        data={"size_bytes": path.stat().st_size},
    )
    db.commit()
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name)
