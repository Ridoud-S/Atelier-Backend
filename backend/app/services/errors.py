"""Domain errors. The API layer maps them to HTTP status codes."""
from __future__ import annotations


class DomainError(Exception):
    code = "domain_error"


class NotFoundError(DomainError):
    """Entity missing OR owned by someone else (indistinguishable on purpose)."""

    code = "not_found"


class ValidationFailed(DomainError):
    code = "validation_failed"


class InvalidTransitionError(DomainError):
    code = "invalid_transition"


class ConflictError(DomainError):
    code = "conflict"
