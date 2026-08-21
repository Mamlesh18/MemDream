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
    orchestrator will not restart the service because the database blipped."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=HealthResponse, summary="Readiness")
async def ready(container: ContainerDep, response: Response) -> HealthResponse:
    """Readiness — every dependency reporting in.

    Three states, not two:

        ok          everything answering
        degraded    something is down that does not stop reads and writes.
                    Still **200**: taking the service out of rotation would be a
                    worse outage than the one being reported
        degraded    a hard dependency is down. **503**, so traffic is withheld

    The middle state is the one worth having. A broken queue means synthesis is
    dead while everything else works, and reporting that as `ok` sends you
    looking at the wrong thing when `POST /v1/dream/synthesize` starts
    returning 502 — which is exactly what happens with `DREAM_QUEUE_PROVIDER=arq`
    and no Redis running.

    Either way the body lists every check, so the answer never requires reading
    logs.
    """
    checks = await container.health()

    def failing(names: tuple[str, ...]) -> list[str]:
        # Only checks that actually report an `ok` field are judged. The
        # `embeddings` and `llm` entries are descriptive, not probes.
        return [
            name
            for name in names
            if "ok" in checks.get(name, {}) and not checks[name]["ok"]
        ]

    # "database" rather than "mongodb": the canonical store is selectable, and a
    # readiness probe keyed to one backend would report healthy on the other no
    # matter what it was doing.
    hard_down = failing(("database", "vector_store"))
    soft_down = failing(("queue",))

    if hard_down:
        response.status_code = 503

    return HealthResponse(
        status="degraded" if (hard_down or soft_down) else "ok",
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
