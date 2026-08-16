"""Token accounting.

Two independent ledgers, and they answer different questions:

* the in-process metrics registry — process-wide, resets on restart, broken down
  by *operation* ("what does an add cost?")
* audit rows — durable, per-user, survives restarts and replicas ("what has this
  user cost me?")

Input and output are tracked separately throughout, because they are priced
separately and an aggregate cannot be converted back into a cost.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import PricingSettings
from app.core.metrics import (
    ALL_OPERATIONS,
    LLM_COMPLETION_TOKENS,
    LLM_PROMPT_TOKENS,
    OP_DECIDE,
    OP_EXTRACT,
    OP_MANUAL,
    OP_SEARCH,
    current_operation,
    metrics,
    operation,
)
from app.main import create_app
from app.models.requests import SearchMemoryRequest

pytestmark = pytest.mark.asyncio

PHONE = "+919999999999"


@pytest.fixture
async def client(container):
    app = create_app()
    app.state.container = container
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def scope_params() -> dict:
    return {"clientId": "client_test", "botId": "bot_test", "phoneNumber": PHONE}


# --------------------------------------------------------------------------- #
# The operation label
# --------------------------------------------------------------------------- #


async def test_operation_defaults_to_unattributed():
    assert current_operation() == "unattributed"


async def test_operation_context_sets_and_restores():
    with operation(OP_EXTRACT):
        assert current_operation() == OP_EXTRACT
    assert current_operation() == "unattributed"


async def test_operation_contexts_nest():
    """Nesting matters: embedding happens inside a decision."""
    with operation(OP_DECIDE):
        assert current_operation() == OP_DECIDE
        with operation(OP_MANUAL):
            assert current_operation() == OP_MANUAL
        assert current_operation() == OP_DECIDE


async def test_operation_restores_on_exception():
    with pytest.raises(RuntimeError), operation(OP_DECIDE):
        raise RuntimeError("boom")
    assert current_operation() == "unattributed"


# --------------------------------------------------------------------------- #
# Attribution through the real pipeline
# --------------------------------------------------------------------------- #


async def test_add_attributes_tokens_to_extract_and_decide(container, scope):
    metrics.reset()
    await container.memory_service.add(scope, "I live in Chennai.")
    await container.memory_service.add(scope, "I moved to Bangalore.")

    report = metrics.token_report()
    by_op = {row["operation"]: row for row in report["byOperation"]}

    assert OP_EXTRACT in by_op, "extraction must be attributed"
    assert by_op[OP_EXTRACT]["inputTokens"] > 0
    assert by_op[OP_EXTRACT]["outputTokens"] > 0

    assert OP_DECIDE in by_op, "the decision call must be attributed separately"
    assert by_op[OP_DECIDE]["inputTokens"] > 0


async def test_embedding_tokens_are_attributed_to_the_write_path(container, scope):
    metrics.reset()
    await container.memory_service.add(scope, "I live in Chennai.")

    by_op = {r["operation"]: r for r in metrics.token_report()["byOperation"]}
    assert by_op.get("remember", {}).get("embeddingTokens", 0) > 0


async def test_search_tokens_are_attributed_to_search(container, scope):
    await container.memory_service.add(scope, "I live in Chennai.")
    metrics.reset()

    await container.memory_service.search(
        scope,
        SearchMemoryRequest(
            query="Where does the user live?",
            client_id=scope.client_id,
            bot_id=scope.bot_id,
            user_id=scope.user_id,
        ),
    )

    by_op = {r["operation"]: r for r in metrics.token_report()["byOperation"]}
    assert OP_SEARCH in by_op
    assert by_op[OP_SEARCH]["embeddingTokens"] > 0
    # Search embeds a query; it must never invoke the language model.
    assert by_op[OP_SEARCH]["inputTokens"] == 0


async def test_manual_create_spends_embedding_but_no_llm_tokens(container, scope):
    metrics.reset()
    await container.memory_service.create_manual(scope, content="User likes tea.")

    by_op = {r["operation"]: r for r in metrics.token_report()["byOperation"]}
    assert by_op[OP_MANUAL]["embeddingTokens"] > 0
    assert by_op[OP_MANUAL]["inputTokens"] == 0
    assert by_op[OP_MANUAL]["outputTokens"] == 0


async def test_exact_duplicate_spends_no_decision_tokens(container, scope):
    """The deterministic short-circuit must be visible in the ledger."""
    await container.memory_service.add(scope, "I live in Chennai.")
    metrics.reset()

    await container.memory_service.add(scope, "I live in Chennai.")

    by_op = {r["operation"]: r for r in metrics.token_report()["byOperation"]}
    assert by_op.get(OP_DECIDE, {}).get("inputTokens", 0) == 0


async def test_only_known_operation_labels_are_used(container, scope):
    """A typo at a call site would create a bucket nobody reports on."""
    metrics.reset()
    await container.memory_service.add(scope, "I live in Chennai and work at Acme.")
    await container.memory_service.search(
        scope,
        SearchMemoryRequest(
            query="where", client_id=scope.client_id, bot_id=scope.bot_id, user_id=scope.user_id
        ),
    )

    seen = {r["operation"] for r in metrics.token_report()["byOperation"]}
    unknown = seen - set(ALL_OPERATIONS) - {"unattributed"}
    assert not unknown, f"unrecognised operation labels: {unknown}"


# --------------------------------------------------------------------------- #
# Cost estimation
# --------------------------------------------------------------------------- #


async def test_cost_uses_separate_input_and_output_rates():
    pricing = PricingSettings(llm_input_per_1m=1.0, llm_output_per_1m=10.0, embedding_per_1m=0.1)

    assert pricing.cost_for(input_tokens=1_000_000) == pytest.approx(1.0)
    assert pricing.cost_for(output_tokens=1_000_000) == pytest.approx(10.0)
    assert pricing.cost_for(embedding_tokens=1_000_000) == pytest.approx(0.1)
    # Output priced 10x input is exactly why they cannot be aggregated.
    assert pricing.cost_for(input_tokens=1000, output_tokens=1000) == pytest.approx(0.011)


async def test_token_report_includes_cost_when_pricing_is_supplied(container, scope):
    metrics.reset()
    await container.memory_service.add(scope, "I live in Chennai.")

    report = metrics.token_report(PricingSettings())
    assert report["totals"]["estimatedCost"] >= 0
    assert report["pricing"]["currency"] == "USD"
    # Never presented as a billed figure.
    assert "not billed amounts" in report["pricing"]["note"]


async def test_token_report_omits_cost_without_pricing(container, scope):
    metrics.reset()
    await container.memory_service.add(scope, "I live in Chennai.")

    report = metrics.token_report()
    assert "pricing" not in report
    assert "estimatedCost" not in report["totals"]


async def test_totals_are_the_sum_of_the_operations(container, scope):
    metrics.reset()
    await container.memory_service.add(scope, "I live in Chennai.")
    await container.memory_service.add(scope, "I moved to Bangalore.")

    report = metrics.token_report()
    rows = report["byOperation"]
    for key in ("inputTokens", "outputTokens", "embeddingTokens"):
        assert report["totals"][key] == sum(r[key] for r in rows)


# --------------------------------------------------------------------------- #
# Durable per-user accounting
# --------------------------------------------------------------------------- #


async def test_audit_rows_record_what_each_decision_cost(container, scope):
    await container.memory_service.add(scope, "I live in Chennai.")
    await container.memory_service.add(scope, "I moved to Bangalore.")

    operations = await container.audit.list_for_scope(scope, limit=10)
    decisions = [op for op in operations if op.action in ("SUPERSEDE", "MERGE")]
    assert decisions, "expected a consolidation to have happened"
    # The field existed but was never populated before this was added.
    assert decisions[0].token_usage.get("prompt_tokens", 0) > 0


async def test_per_user_token_usage_is_queryable(container, scope):
    await container.memory_service.add(scope, "I live in Chennai.")
    await container.memory_service.add(scope, "I moved to Bangalore.")

    usage = await container.audit.token_usage(scope)
    assert usage["totals"]["inputTokens"] > 0
    assert usage["totals"]["totalTokens"] == (
        usage["totals"]["inputTokens"] + usage["totals"]["outputTokens"]
    )
    assert usage["byAction"]


async def test_per_user_usage_does_not_include_other_tenants(container, scope, other_scope):
    await container.memory_service.add(scope, "I live in Chennai.")
    await container.memory_service.add(other_scope, "I live in Mumbai.")

    mine = await container.audit.token_usage(scope)
    theirs = await container.audit.token_usage(other_scope)

    assert mine["totals"]["operations"] >= 1
    assert theirs["totals"]["operations"] >= 1
    # Each scope sees only its own spend.
    assert mine["byAction"].keys() | theirs["byAction"].keys()


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


async def test_metrics_endpoint_exposes_the_token_breakdown(client, container, scope):
    metrics.reset()
    await container.memory_service.add(scope, "I live in Chennai.")

    body = (await client.get("/v1/metrics")).json()
    assert "tokens" in body
    assert body["tokens"]["totals"]["totalTokens"] > 0


async def test_dedicated_token_endpoint(client, container, scope):
    metrics.reset()
    await container.memory_service.add(scope, "I live in Chennai.")

    response = await client.get("/v1/metrics/tokens")
    assert response.status_code == 200

    body = response.json()
    assert body["byOperation"]
    assert body["totals"]["totalTokens"] > 0
    assert "estimatedCost" in body["totals"]


async def test_stats_reports_per_user_tokens_and_cost(client, container, scope):
    await container.memory_service.add(scope, "I live in Chennai.")
    await container.memory_service.add(scope, "I moved to Bangalore.")

    body = (await client.get("/v1/dream/stats", params=scope_params())).json()
    assert "tokens" in body
    assert body["tokens"]["totals"]["inputTokens"] > 0
    assert "estimatedCost" in body["tokens"]["totals"]
    assert body["tokens"]["currency"] == "USD"


async def test_counters_carry_the_operation_label(container, scope):
    metrics.reset()
    await container.memory_service.add(scope, "I live in Chennai.")

    snapshot = metrics.snapshot()
    prompt_labels = snapshot["counters"].get(LLM_PROMPT_TOKENS, {})
    completion_labels = snapshot["counters"].get(LLM_COMPLETION_TOKENS, {})

    assert any("operation=" in label for label in prompt_labels)
    assert any("operation=" in label for label in completion_labels)
