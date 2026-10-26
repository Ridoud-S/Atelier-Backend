"""Cryptographic verification of Supabase Auth access tokens.

The backend NEVER trusts a `user_id` sent by the client: the only source of identity is the
`sub` claim of a token whose signature, issuer, audience and expiry were verified here.

Supabase signs tokens either with an asymmetric key published as a JWKS (ES256/RS256, the
recommended setup) or, in legacy projects, with a shared HS256 secret. The algorithm list is
fixed by configuration and is never read from the token header (prevents `alg=none` and
algorithm-confusion attacks).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient

from app.core.config import Settings


class TokenError(Exception):
    """The token is missing, malformed, expired or not trusted. Message is safe to log."""


@dataclass(frozen=True)
class AuthenticatedUser:
    id: uuid.UUID
    email: str | None = None


class KeyProvider(Protocol):
    algorithms: list[str]

    def key_for(self, token: str) -> Any: ...


class JWKSKeyProvider:
    """Public keys fetched (and cached) from the project's JWKS endpoint."""

    algorithms = ["ES256", "RS256"]

    def __init__(self, jwks_url: str) -> None:
        self._client = PyJWKClient(jwks_url, cache_keys=True, lifespan=600)

    def key_for(self, token: str) -> Any:
        try:
            return self._client.get_signing_key_from_jwt(token).key
        except jwt.PyJWTError as exc:
            raise TokenError("signing key not found for token") from exc


class StaticKeyProvider:
    """A single configured key (HS256 shared secret in legacy projects; fixed keys in tests)."""

    def __init__(self, key: Any, algorithms: list[str]) -> None:
        if not key:
            raise ValueError("a verification key is required")
        self._key = key
        self.algorithms = list(algorithms)

    def key_for(self, token: str) -> Any:  # noqa: ARG002
        return self._key


class TokenVerifier:
    def __init__(self, key_provider: KeyProvider, issuer: str, audience: str = "authenticated") -> None:
        if not issuer:
            raise ValueError("the expected token issuer must be configured")
        self._keys = key_provider
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str) -> AuthenticatedUser:
        try:
            key = self._keys.key_for(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self._keys.algorithms,  # fixed list, never from the header
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except TokenError:
            raise
        except jwt.PyJWTError as exc:
            raise TokenError(f"invalid token: {type(exc).__name__}") from exc

        if claims.get("role") != "authenticated":
            raise TokenError("token is not an authenticated-user token")
        try:
            user_id = uuid.UUID(str(claims["sub"]))
        except ValueError as exc:
            raise TokenError("token subject is not a UUID") from exc
        return AuthenticatedUser(id=user_id, email=claims.get("email"))


def build_verifier(settings: Settings) -> TokenVerifier:
    """Create the production verifier from configuration (fails loudly if misconfigured)."""
    if settings.supabase_jwks_url:
        provider: KeyProvider = JWKSKeyProvider(settings.supabase_jwks_url)
    elif settings.supabase_jwt_secret:
        provider = StaticKeyProvider(settings.supabase_jwt_secret, ["HS256"])
    else:
        raise RuntimeError("configure SUPABASE_JWKS_URL (preferred) or SUPABASE_JWT_SECRET")
    return TokenVerifier(provider, issuer=settings.supabase_jwt_issuer, audience=settings.supabase_jwt_audience)
