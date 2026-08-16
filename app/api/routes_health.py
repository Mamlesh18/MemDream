"""Health, readiness and metrics."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app import __version__
from app.api.deps import ContainerDep
from app.core.metrics import metrics
from app.models.requests import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness")
async def health() -> HealthResponse:
    """Liveness only — the process is up. Does not touch dependencies, so an
    orchestrator will not restart the service because Mongo blipped."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=HealthResponse, summary="Readiness")
async def ready(container: ContainerDep, response: Response) -> HealthResponse:
    """Readiness — every dependency reporting in.

    Returns 503 when a hard dependency is down, so traffic is withheld, but the
    body still lists which one, so the answer does not require reading logs.
    """
    checks = await container.health()

    hard_dependencies = ("mongodb", "vector_store")
    degraded = [
        name
        for name in hard_dependencies
        if not checks.get(name, {}).get("ok", False)
    ]
    if degraded:
        response.status_code = 503

    return HealthResponse(
        status="degraded" if degraded else "ok",
        version=__version__,
        checks=checks,
    )


@router.get("/v1/metrics", summary="Metrics snapshot")
async def get_metrics(container: ContainerDep) -> dict:
    """Counters, latency histograms, and token accounting.

    Deliberately not the Prometheus text format: with `DREAM_OTEL_ENABLED=true`
    the same instruments export over OTLP, which is the path meant for real
    scraping. This endpoint is for eyeballing a running instance.

    The `tokens` section breaks spend down by operation, which is what makes
    "what does one `add` actually cost?" answerable. Input and output are
    reported separately because they are priced separately.
    """
    snapshot = metrics.snapshot()
    snapshot["tokens"] = metrics.token_report(container.settings.pricing)
    return snapshot


@router.get("/v1/metrics/tokens", summary="Token usage and estimated cost")
async def get_token_metrics(container: ContainerDep) -> dict:
    """Token spend by operation, since this process started.

    Process-wide and in-memory: it resets on restart, and a second replica keeps
    its own counts. For per-user, durable accounting use `GET /v1/dream/stats`,
    which sums the token usage recorded on each audit row.
    """
    return metrics.token_report(container.settings.pricing)
