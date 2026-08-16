"""FastAPI application.

Run with:

    uvicorn app.main:app --reload
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import routes_dream, routes_health, routes_memory, routes_search
from app.container import DreamContainer
from app.core.config import get_settings
from app.core.exceptions import DreamError
from app.core.logging import (
    clear_scope,
    configure_logging,
    get_logger,
    new_request_id,
    set_request_id,
)

logger = get_logger(__name__)

DESCRIPTION = """
A self-maintaining long-term memory layer for AI agents.

**Remember** — `POST /v1/memories` extracts durable facts from a message and lets
the Dream layer decide whether to add, supersede, merge, or do nothing.

**Recall** — `POST /v1/memories/search` returns hybrid-ranked memories, scoped to
one client, bot and user.

**Dream** — `POST /v1/dream/synthesize` queues background synthesis, which finds
patterns across many memories and records what evidence they rest on.

Memories are never deleted. Superseded and merged records are retained with
pointers to whatever replaced them, so history stays answerable.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    logger.info("dream_memory_starting", version=__version__, env=settings.env)

    container = await DreamContainer.create(settings)
    app.state.container = container

    try:
        yield
    finally:
        logger.info("dream_memory_stopping")
        await container.close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title="Dream Memory",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Wide open by default: this service is meant to sit behind a gateway on a
    # private network. Narrow it before exposing it publicly.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Attach a request id and log the outcome of every request."""
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = request_id
        set_request_id(request_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        finally:
            # Scope is bound per request; leaving it set would leak one user's
            # identifiers into the next request handled by this worker.
            clear_scope()
            set_request_id(None)

        duration_ms = (time.perf_counter() - started) * 1000.0
        response.headers["X-Request-ID"] = request_id
        # Health checks are high-volume and uninteresting.
        if request.url.path not in ("/health", "/ready"):
            logger.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round(duration_ms, 2),
                request_id=request_id,
            )
        return response

    @app.exception_handler(DreamError)
    async def dream_error_handler(request: Request, exc: DreamError) -> JSONResponse:
        log = logger.error if exc.status_code >= 500 else logger.info
        log(
            "dream_error",
            code=exc.code,
            message=exc.message,
            path=request.url.path,
            status=exc.status_code,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Project each error down to the three JSON-safe fields. Pydantic puts
        # the original exception object in `ctx`, which will not serialise, and
        # `input` echoes the whole request body — including the user's message —
        # back into an error response, which is not something to do by default.
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "message": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request failed validation",
                    "details": {"errors": errors},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", path=request.url.path, error=str(exc)[:500])
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    # The message is deliberately generic: exception text can
                    # carry connection strings and prompt content.
                    "message": "An unexpected error occurred",
                    "details": {"requestId": getattr(request.state, "request_id", None)},
                }
            },
        )

    app.include_router(routes_health.router)
    app.include_router(routes_memory.router)
    app.include_router(routes_search.router)
    app.include_router(routes_dream.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "service": "dream-memory",
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = create_app()
