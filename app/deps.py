import hmac

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_session
from .models import Application
from .security import hash_api_key


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    return authorization.removeprefix("Bearer ").strip()


def require_admin(authorization: str | None = Header(default=None)) -> None:
    token = _bearer(authorization)
    expected = get_settings().admin_token
    # An empty admin token disables app creation instead of accepting "".
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin token required")


def current_app(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Application:
    key = _bearer(authorization)
    app = session.scalar(select(Application).where(Application.api_key_hash == hash_api_key(key)))
    if app is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key",
                            headers={"WWW-Authenticate": "Bearer"})
    return app
