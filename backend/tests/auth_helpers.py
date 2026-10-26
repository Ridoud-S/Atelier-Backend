"""Local signing keys and token forging helpers (the real Supabase is never contacted)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ISSUER = "https://test-project.supabase.co/auth/v1"
AUDIENCE = "authenticated"


def new_ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def private_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def public_pem(key) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def claims(_sub, **overrides) -> dict:
    now = int(time.time())
    base = {
        "sub": str(_sub),
        "aud": AUDIENCE,
        "iss": ISSUER,
        "role": "authenticated",
        "iat": now,
        "exp": now + 3600,
        "email": "user@example.com",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def sign_es256(key, payload: dict, kid: str = "test-key") -> str:
    return jwt.encode(payload, private_pem(key), algorithm="ES256", headers={"kid": kid})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forge_hs256_with_public_key(public_key_pem: bytes, payload: dict) -> str:
    """Algorithm-confusion attack: HS256 token whose HMAC secret is the (public) PEM key."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    signing_input = f"{header}.{body}".encode()
    signature = hmac.new(public_key_pem, signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(signature)}"


def unsigned_token(payload: dict) -> str:
    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    return f"{header}.{_b64(json.dumps(payload).encode())}."


def tamper_subject(token: str, new_sub) -> str:
    """Replace the payload (keeping header and signature) to impersonate someone else."""
    header, body, signature = token.split(".")
    padded = body + "=" * (-len(body) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded))
    data["sub"] = str(new_sub)
    return f"{header}.{_b64(json.dumps(data).encode())}.{signature}"


def random_sub() -> uuid.UUID:
    return uuid.uuid4()
