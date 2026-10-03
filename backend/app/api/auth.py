from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.config import get_settings
from app.db import get_db
from app.deps import PathId, get_current_user, require_role
from app.models import User, UserRole
from app.schemas.api import (
    LoginIn,
    PasswordChange,
    PasswordReset,
    SetupIn,
    SetupStatus,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.security import COOKIE_NAME, create_access_token, hash_password, verify_password
from app.services import login_guard

router = APIRouter(prefix="/api", tags=["auth"])


def _set_session(response: Response, user: User) -> None:
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        create_access_token(user.id, user.session_version),
        max_age=settings.access_token_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def _user_count(db: Session) -> int:
    return db.scalar(select(func.count(User.id))) or 0


@router.get("/auth/setup", response_model=SetupStatus)
def setup_status(db: Session = Depends(get_db)) -> SetupStatus:
    return SetupStatus(needs_setup=_user_count(db) == 0)


@router.post("/auth/setup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def first_run_setup(body: SetupIn, response: Response, db: Session = Depends(get_db)) -> User:
    """Creates the first admin. Only allowed while there are no users."""
    if _user_count(db) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed")
    user = User(
        email=body.email.lower(),
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        role=UserRole.ADMIN,
    )
    db.add(user)
    db.flush()
    audit.record(
        db, action="user.setup_admin", entity_type="user", entity_id=user.id, actor_id=user.id
    )
    db.commit()
    _set_session(response, user)
    return user


@router.post("/auth/login", response_model=UserOut)
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)) -> User:
    email = body.email.lower()
    if login_guard.blocked(email):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed sign-ins. Wait 15 minutes and try again, or ask an administrator "
            "to reset your password.",
        )
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(user.password_hash, body.password):
        login_guard.failed(email)
        audit.record(
            db,
            action="user.login_failed",
            entity_type="user",
            entity_id=user.id if user else "unknown",
            data={"email": email},
        )
        db.commit()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "The email or password is not correct. Check both and try again.",
        )
    login_guard.succeeded(email)
    # Only someone who knows the password learns this, and it tells them who can help.
    if not user.is_active:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account has been turned off. Ask an admin in your office to turn it back on.",
        )
    audit.record(db, action="user.login", entity_type="user", entity_id=user.id, actor_id=user.id)
    db.commit()
    _set_session(response, user)
    return user


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


@router.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_role())) -> list[User]:
    return list(db.scalars(select(User).order_by(User.id)))


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate, db: Session = Depends(get_db), admin: User = Depends(require_role())
) -> User:
    email = body.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with this email already exists")
    user = User(
        email=email,
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    db.flush()
    audit.record(
        db,
        action="user.created",
        entity_type="user",
        entity_id=user.id,
        actor_id=admin.id,
        data={"email": email, "role": body.role},
    )
    db.commit()
    return user


def _get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


def _active_admins(db: Session) -> int:
    return (
        db.scalar(
            select(func.count(User.id)).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
        or 0
    )


def _check_update_allowed(user: User, admin: User, changes: dict) -> None:
    if user.id == admin.id and user.is_active and changes.get("is_active") is False:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You can't deactivate your own account. Ask another admin to do it.",
        )


def _keep_an_admin(db: Session) -> None:
    """Counted after the change is written, inside its transaction: the write holds the
    database lock, so two admins stepping down at once can't both pass a check made first."""
    db.flush()
    if _active_admins(db) == 0:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This is the only active admin. Make another user an admin first, "
            "then change this one.",
        )


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: PathId,
    body: UserUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role()),
) -> User:
    user = _get_user(db, user_id)
    # A null field means "leave as is": none of these can be empty.
    sent = body.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    changes = {k: v for k, v in sent.items() if getattr(user, k) != v}
    if not changes:
        return user
    _check_update_allowed(user, admin, changes)
    before = {k: getattr(user, k) for k in changes}
    for key, value in changes.items():
        setattr(user, key, value)
    if changes.get("is_active") is False:
        user.session_version += 1  # signs them out everywhere, also after reactivation
    _keep_an_admin(db)
    audit.record(
        db,
        action="user.updated",
        entity_type="user",
        entity_id=user.id,
        actor_id=admin.id,
        data={"before": before, "after": changes},
    )
    db.commit()
    return user


@router.post("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    user_id: PathId,
    body: PasswordReset,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role()),
) -> None:
    user = _get_user(db, user_id)
    user.password_hash = hash_password(body.password)
    user.session_version += 1  # whoever had the old password is signed out
    audit.record(
        db, action="user.password_reset", entity_type="user", entity_id=user.id, actor_id=admin.id
    )
    db.commit()


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: PasswordChange,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Your current password is not correct. Type it again to change your password.",
        )
    user.password_hash = hash_password(body.new_password)
    user.session_version += 1  # other browsers are signed out; this one gets a new session
    audit.record(
        db, action="user.password_changed", entity_type="user", entity_id=user.id, actor_id=user.id
    )
    db.commit()
    _set_session(response, user)
