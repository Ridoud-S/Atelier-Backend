"""FastAPI dependencies: authentication and database session."""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import AuthenticatedUser, TokenError, TokenVerifier, build_verifier
from app.db.session import get_session
from app.services.provisioning import ensure_profile

_bearer = HTTPBearer(auto_error=False)


@lru_cache
def get_token_verifier() -> TokenVerifier:
    return build_verifier(get_settings())


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    verifier: TokenVerifier = Depends(get_token_verifier),
) -> AuthenticatedUser:
    """Identity comes ONLY from the verified token."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    try:
        return verifier.verify(credentials.credentials)
    except TokenError:
        raise unauthorized from None


class RequestContext:
    """The authenticated user id plus the session every query must be scoped with."""

    def __init__(self, user: AuthenticatedUser, session: Session) -> None:
        self.user = user
        self.session = session

    @property
    def user_id(self):
        return self.user.id


def get_context(
    user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Iterator[RequestContext]:
    ensure_profile(session, user.id)  # first access creates profile, settings, game profile
    session.commit()
    yield RequestContext(user, session)
