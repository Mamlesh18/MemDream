---
name: llm-and-prompts
description: Use when calling an LLM or changing AI behaviour in this repo — Azure OpenAI client setup, platform config and model master resolution, intent classification, system prompts and their Redis caches, conversation history, AI credit checks and token recording, or translation.
---

# LLM calls and prompts

Related: [[usage-enforcement]], [[data-access]], [[logging]], [[dialog]].

## Two client paths

| Path | Used by | Notes |
|---|---|---|
| `LlmService` (platform singleton) | `classify()`, `detectAosId()` — AOS routing, small classifications | One Azure OpenAI client for all bots; per-call config comes from the caller's `DataConfigService`, so the singleton holds no tenant state |
| `initializeAzureOpenAI(platformConfig, dataConfigService)` | `IntentService`, `ImageExtractionService`, `MainDialog` | Per-service client |

Prefer `LlmService.getInstance().classify(...)` for new small LLM calls. Don't create `new AzureOpenAI(...)` anywhere else.

## Model resolution chain

Never hardcode an endpoint, deployment or key. Every client is built the same way:

```
platform config by name ("INTENT-CLASSIFICATION", …)
  → details.find(d => d.enabled)
  → modelId → dataConfigService.getModelMasterById(modelId)
  → checks: status ACTIVE, type COMPLETION, provider azure-openai
  → apiKey = KeyVaultService.getSecret(modelMaster.key)
  → new AzureOpenAI({ endpoint, apiKey, deployment: deploymentName, apiVersion })
```

- Every failure in that chain logs a **Warning** and leaves the client undefined. Callers must handle "no client" by degrading (skip the AI step, use the rule-based path), never by throwing at the customer.
- `classify()` returns `null` when the client is missing or the call fails. Always handle `null`.
- Call parameters come from the enabled config with these defaults: `maxTokens` 200, `temperature` 0, `topP` 1. Keep `temperature: 0` for anything classification-like.

## AI credits — required around every LLM call

```ts
const aiGate = await this.enforcementService.preCheckAiCredits(correlationId);
if (!aiGate.allowed) { /* skip the LLM, fall back */ }

const result = await this.llmService.classify(dataConfigService, systemPrompt, userText, opts);

await this.enforcementService.recordLlmUsageEvent({
  llmKey, correlationId,
  metrics: [{ metricKey: 'inputTokens', value: result?.usage?.prompt_tokens ?? 0 }, ...],
});
```

Pre-check before, record after success, recording is non-blocking. Follow `intent.service.ts` and `emailClassificationService.ts`. See [[usage-enforcement]].

## Prompts
- Build prompts from config and the LG template, not from strings scattered in dialogs. Helpers: `utils/prompt.ts` (`promptHelper`), `prompt.service.ts`, `prompt-generation.service.ts`.
- Derived system prompts are cached in Redis as `<botId>:systemPrompt` and `<botId>:systemPrompt:whatsapp`.
- **Invalidate on config change.** `DataConfigService.refreshPlatformConfig()` deletes those keys and calls `LlmService.resetClient()`. It runs from `POST /api/config/callback` (`PLATFORM_CONFIG`) and `POST /api/reset`. A new derived cache must be cleared there too, or tenants keep serving stale prompts. See [[multitenancy]].
- When the model must choose from a list, show it human-readable titles and map the answer back to ids yourself (`detectAosId`). Don't ask for opaque ids, and validate whatever comes back against the candidate set.
- Write explicit, bounded instructions in the system prompt, and state the exact output shape you will parse.

## Conversation history and input size
- `MessageService.getConversationHistory(userId, botId, n)` — keep `n` small (AOS routing uses 10).
- Clean inputs before sending: `cleanEmailBody` strips quoted reply chains, which both wastes tokens and lets an old quoted message drive the answer.
- Never send whole payloads, attachments or channel metadata to the model.

## Translation
`AiTranslateService.translate(activity)` sends an external request per item (text, button titles, card fields) with per-item ids and reapplies the results. Add new translatable surfaces by pushing another item with its own `apply`, not by translating the whole JSON blob.

## Logging
- Never log prompts, completions or customer text. Log the decision and identifiers: `intent`, `aosId`, `isRelevant`, `correlationId`, token counts.
- Use `Modules.INTENT_SERVICE` (or the calling feature's module) with `INTENT_SERVICE_ERROR`. See [[logging]].

## Checklist
- [ ] Client built through platform config + model master + Key Vault; nothing hardcoded
- [ ] `null` / missing-client path degrades gracefully
- [ ] `preCheckAiCredits` before, `recordLlmUsageEvent` after
- [ ] New derived caches invalidated in `refreshPlatformConfig`
- [ ] Model output validated against a known candidate set
- [ ] No prompts, completions or customer text in logs
