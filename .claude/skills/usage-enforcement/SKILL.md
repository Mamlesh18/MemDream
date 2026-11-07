---
name: usage-enforcement
description: Use when working on usage limits, quotas, or billing metering — EnforcementService pre-checks and recording, conversation limits, AI credits, ConversationLimitExceededError and 429 responses, the daily user limit middleware, or metering a new feature.
---

# Usage enforcement and limits

Two separate systems. Don't confuse them.

| System | Where | Scope |
|---|---|---|
| **EnforcementService** | `src/services/enforcement.service.ts` | External billing service: conversation counts and AI credits per tenant |
| **DailyUserLimitMiddleware** | `src/middleware/dailyUserLimit.middleware.ts` | Per-bot `botConfig.customTenantUsageLimits.maxUsersPerDay` |

Related: [[llm-and-prompts]], [[error-handling]], [[bot-middleware]].

## EnforcementService

Config: `ENFORCEMENT_SERVICE_DIRECT_URL` or `ENFORCEMENT_BASE_URL`, `ENFORCEMENT_ENABLED`, `ENFORCEMENT_FAIL_OPEN`, `ENFORCEMENT_TIMEOUT_SECONDS` (default 10). Every call sends `tenantId` (resolved client id), `appId` (botId) and `productLine: 'engage-ai'`.

### Fail-open is deliberate
- Not configured, or `ENFORCEMENT_ENABLED=false` → `{ allowed: true, message: 'enforcement_disabled' }`.
- Unavailable or timing out, with `failOpen` on (the default) → allowed, with a message naming the reason.
- Only when `ENFORCEMENT_FAIL_OPEN=false` does an error deny.

A billing-service outage must not stop customer conversations. Keep this behaviour, and keep the reason strings — they're how a fail-open is spotted in telemetry.

### The four calls

| Call | When | Notes |
|---|---|---|
| `preCheckNewConversation()` | Before a new conversation is created (`user.middleware`) | Check only, records nothing |
| `recordNewConversation(conversationId)` | Before the Mongo insert (`conversation.service`) | Authoritative increment; `idempotencyKey` = conversationId |
| `preCheckAiCredits()` | Before **every** LLM call | Skip the LLM when denied |
| `recordLlmUsageEvent({ llmKey, metrics, correlationId })` | After a successful LLM call | Non-blocking, returns `null` on failure; zero-value metrics are dropped |

Pattern for anything metered: **pre-check → do the work → record what really happened.** Never record before the work, and never record twice for one unit of work — pass a stable `idempotencyKey`.

### Denials
- `ConversationLimitExceededError` carries the `CheckAndRecordResult`. Check it with `instanceof`, never by message text.
- `user.middleware` normally absorbs it and shows the limit card from the LG template.
- The email adapter's `onTurnError` swallows it: the denial is deterministic, so a retry would just replay it.
- `POST /api/agent` surfaces the denial recorded in `turnState` as a structured **429**.
- A conversation already in progress is exempt: `isInOngoingConversation` / `getConversationExpirySeconds` (`botConfig.conversation.expiryInSeconds`, default 4 hours). A conversation that was allowed to start must be allowed to finish.

## DailyUserLimitMiddleware
- Reads `botConfig` from `turnState` (set by `UserMiddleware`), so it costs no extra DB read.
- Skips entirely when `maxUsersPerDay` is missing or `enabled === false`.
- Caches the verdict on the user document for the day (`accessDate`, `isWithinDailyLimit`) to avoid re-querying.
- When over the limit it sends the limit-reached card and **stops the turn** (no `next()`).
- Registered last in the middleware chain on every adapter. See [[bot-middleware]].

## Metering a new feature
1. Add the metric key to the metrics map alongside the existing `engage-ai` keys.
2. Pre-check at the point where you can still degrade gracefully.
3. Record after success, with a stable `correlationId` (and `idempotencyKey` for counted units).
4. Keep it to **one enforcement call per turn or per request** — never inside a loop over messages or attachments.
5. Log with the calling feature's `Modules` value, and include `correlationId`. See [[logging]].

## Checklist
- [ ] Fail-open preserved unless the deny path is explicitly configured
- [ ] Pre-check before the work, record after, exactly once
- [ ] Idempotency key for counted units
- [ ] Denials raised or surfaced through the existing error type and 429 shape
- [ ] In-progress conversations stay exempt
- [ ] No enforcement calls in hot loops
