"""Cryptographic verification of Supabase tokens (signature, issuer, audience, expiry, ...)."""
from __future__ import annotations

import http.server
import json
import threading
import time
import uuid

import jwt
import pytest

from app.core.config import Settings
from app.core.security import (
    JWKSKeyProvider,
    StaticKeyProvider,
    TokenError,
    TokenVerifier,
    build_verifier,
)
from tests import auth_helpers as ah


@pytest.fixture(scope="module")
def key():
    return ah.new_ec_key()


@pytest.fixture(scope="module")
def verifier(key):
    return TokenVerifier(StaticKeyProvider(key.public_key(), ["ES256"]), issuer=ah.ISSUER, audience=ah.AUDIENCE)


def test_a_valid_token_yields_the_subject_as_the_user_id(verifier, key):
    sub = uuid.uuid4()
    user = verifier.verify(ah.sign_es256(key, ah.claims(sub)))
    assert user.id == sub and user.email == "user@example.com"


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 10},  # expired
        {"nbf": int(time.time()) + 600},  # not valid yet
        {"aud": "anon-clients"},  # wrong audience
        {"aud": None},  # no audience
        {"iss": "https://evil.example.com/auth/v1"},  # wrong issuer
        {"iss": None},
        {"sub": None},  # no subject
        {"sub": "not-a-uuid"},
        {"exp": None},  # a token that never expires is refused
        {"role": "anon"},
        {"role": "service_role"},
        {"role": None},
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items()),
)
def test_untrusted_claims_are_rejected(verifier, key, overrides):
    token = ah.sign_es256(key, ah.claims(uuid.uuid4(), **overrides))
    with pytest.raises(TokenError):
        verifier.verify(token)


def test_a_token_signed_by_another_key_is_rejected(verifier):
    attacker = ah.new_ec_key()
    with pytest.raises(TokenError):
        verifier.verify(ah.sign_es256(attacker, ah.claims(uuid.uuid4())))


def test_changing_the_payload_invalidates_the_signature(verifier, key):
    token = ah.sign_es256(key, ah.claims(uuid.uuid4()))
    with pytest.raises(TokenError):
        verifier.verify(ah.tamper_subject(token, uuid.uuid4()))


def test_alg_none_is_rejected(verifier):
    with pytest.raises(TokenError):
        verifier.verify(ah.unsigned_token(ah.claims(uuid.uuid4())))


def test_algorithm_confusion_with_the_public_key_as_hmac_secret_is_rejected(verifier, key):
    forged = ah.forge_hs256_with_public_key(ah.public_pem(key), ah.claims(uuid.uuid4()))
    with pytest.raises(TokenError):
        verifier.verify(forged)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "Bearer x", "....", "é.é.é"])
def test_garbage_is_rejected(verifier, garbage):
    with pytest.raises(TokenError):
        verifier.verify(garbage)


def test_the_error_does_not_leak_why_beyond_the_exception_class(verifier, key):
    token = ah.sign_es256(key, ah.claims(uuid.uuid4(), exp=int(time.time()) - 10))
    with pytest.raises(TokenError) as info:
        verifier.verify(token)
    assert "ExpiredSignatureError" in str(info.value) and "eyJ" not in str(info.value)  # no token echo


def test_a_verifier_without_an_issuer_cannot_be_built(key):
    with pytest.raises(ValueError):
        TokenVerifier(StaticKeyProvider(key.public_key(), ["ES256"]), issuer="")


# --- legacy HS256 shared secret -------------------------------------------------------------------
def test_hs256_shared_secret_projects():
    secret = "x" * 48
    v = TokenVerifier(StaticKeyProvider(secret, ["HS256"]), issuer=ah.ISSUER)
    good = jwt.encode(ah.claims(uuid.uuid4()), secret, algorithm="HS256")
    assert v.verify(good).id
    with pytest.raises(TokenError):
        v.verify(jwt.encode(ah.claims(uuid.uuid4()), "y" * 48, algorithm="HS256"))


def test_an_hs256_verifier_does_not_accept_es256_tokens(key):
    v = TokenVerifier(StaticKeyProvider("x" * 48, ["HS256"]), issuer=ah.ISSUER)
    with pytest.raises(TokenError):
        v.verify(ah.sign_es256(key, ah.claims(uuid.uuid4())))


# --- configuration ----------------------------------------------------------------------------------
def test_build_verifier_refuses_to_start_unconfigured():
    with pytest.raises(RuntimeError, match="SUPABASE_JWKS_URL"):
        build_verifier(Settings(supabase_jwt_issuer=ah.ISSUER))


def test_build_verifier_requires_an_issuer():
    with pytest.raises(ValueError):
        build_verifier(Settings(supabase_jwt_secret="s" * 40, supabase_jwt_issuer=""))


# --- JWKS end to end (served by a local HTTP server) --------------------------------------------------
@pytest.fixture
def jwks_server(key):
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "test-key", "use": "sig", "alg": "ES256"})
    body = json.dumps({"keys": [jwk]}).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/jwks.json"
    server.shutdown()


def test_jwks_verification_end_to_end(jwks_server, key):
    verifier = TokenVerifier(JWKSKeyProvider(jwks_server), issuer=ah.ISSUER)
    sub = uuid.uuid4()
    assert verifier.verify(ah.sign_es256(key, ah.claims(sub), kid="test-key")).id == sub


def test_jwks_unknown_key_id_is_rejected(jwks_server, key):
    verifier = TokenVerifier(JWKSKeyProvider(jwks_server), issuer=ah.ISSUER)
    with pytest.raises(TokenError):
        verifier.verify(ah.sign_es256(key, ah.claims(uuid.uuid4()), kid="someone-elses-key"))


def test_jwks_rejects_a_token_from_another_key_even_with_a_known_kid(jwks_server):
    verifier = TokenVerifier(JWKSKeyProvider(jwks_server), issuer=ah.ISSUER)
    attacker = ah.new_ec_key()
    with pytest.raises(TokenError):
        verifier.verify(ah.sign_es256(attacker, ah.claims(uuid.uuid4()), kid="test-key"))


def test_jwks_endpoint_down_means_no_authentication():
    verifier = TokenVerifier(JWKSKeyProvider("http://127.0.0.1:9/jwks.json"), issuer=ah.ISSUER)
    attacker = ah.new_ec_key()
    with pytest.raises(TokenError):
        verifier.verify(ah.sign_es256(attacker, ah.claims(uuid.uuid4())))
