from collections.abc import Callable
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Company, User, UserRole
from app.security import COOKIE_NAME, decode_access_token

# Database ids are signed 64-bit; a larger number in a URL would crash the query (a 500)
# instead of giving a clean "not found".
MAX_ID = 2**63 - 1
PathId = Annotated[int, Path(ge=1, le=MAX_ID)]
QueryId = Annotated[int | None, Query(ge=1, le=MAX_ID)]


def get_current_user(
    db: Session = Depends(get_db),
    token: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    claims = decode_access_token(token) if token else None
    user = db.get(User, claims[0]) if claims else None
    if user is None or not user.is_active or user.session_version != claims[1]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return user


def require_role(*roles: UserRole) -> Callable[..., User]:
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role != UserRole.ADMIN and user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return user

    return checker


def get_company(
    company_id: PathId, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company
