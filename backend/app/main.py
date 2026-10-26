"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_token_verifier
from app.api.v1.routes import router as v1_router
from app.core.config import get_settings
from app.services.errors import (
    ConflictError,
    DomainError,
    InvalidTransitionError,
    NotFoundError,
    ValidationFailed,
)

logger = logging.getLogger("atelier")

_STATUS = {
    NotFoundError: 404,
    ValidationFailed: 422,
    InvalidTransitionError: 409,
    ConflictError: 409,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast: a server that cannot verify tokens must not start (instead of answering 500
    # on the first authenticated request). Tests that override the verifier skip this.
    if get_token_verifier not in app.dependency_overrides:
        get_token_verifier()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.include_router(v1_router, prefix=settings.api_v1_prefix)

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=_STATUS.get(type(exc), 400))

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        # Constraint violations are logged server-side; the client gets no schema details.
        logger.warning("integrity error: %s", exc.orig)
        return JSONResponse({"error": "integrity_error", "detail": "request violates a data rule"}, status_code=409)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name}

    return app


app = create_app()
