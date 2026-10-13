---
name: api-endpoint
description: Use when adding or changing an Express HTTP route in src/index.ts — any /api/* endpoint, channel webhook, or agent-facing API. Covers header validation, bot instance lookup, the internal API key guard, response shapes, error logging, and the required openapi.json update.
---

# Writing an API endpoint

All routes live in `src/index.ts`, registered on `app` inside the server start-up block. There is no router split. Put a new route next to the routes of the same feature (email with email, call with call) and give it a short comment like its neighbours.

Related: [[error-handling]], [[logging]], [[service]].

## Template

```ts
app.post("/api/<feature>/<action>", async (req: Request, res: Response) => {
  const botId = req.headers["bot-id"] as string;
  try {
    // 1. Auth — agent-facing routes only (see "Auth" below)
    if (!requireInternalAgentApiKey(req, res)) return;

    // 2. Validate input → 400 { error }
    const agentId = req.headers["agent-id"] as string;
    if (!botId) return res.status(400).json({ error: "Missing bot-id header" });
    if (!agentId) return res.status(400).json({ error: "Missing agent-id header" });

    // 3. Resolve the tenant
    const botInstance = await getOrCreateBotInstance(botId);

    // 4. Delegate to a service — no business logic or DB queries in the route
    const updated = await botInstance.threadService.setPaused(String(req.params.threadId), true);
    if (!updated) {
      return res.status(404).json({ error: `Thread ${req.params.threadId} not found for bot ${botId}` });
    }

    // 5. Success
    return res.status(200).json({ threadId: updated.threadId, isPaused: updated.isPaused });
  } catch (err: any) {
    console.error("[api/<feature>/<action>] Error:", err);
    new Logger(telemetryClient, botId || "unknown", botInstances.get(botId)?.dataConfig.clientId).error(
      err,
      "/api/<feature>/<action>",
      "Error <doing the thing>",
      Modules.THREAD_SERVICE,
      ERROR_CODE.UPDATE_THREAD_FAILED,
    );
    return res.status(500).json({ error: err?.message || "Failed to <do the thing>" });
  }
});
```

## Rules

**Inputs**
- Tenant and actor ids come from kebab-case headers: `bot-id`, `agent-id`, `user-id`, `thread-id`. Resource ids go in the path (`:threadId`, `:messageId`). Channel webhooks take `:botId` in the path.
- These headers are routing identifiers, **not authentication**. Never trust them for authorization.
- Validate every required header, param and body field before doing any work. Each missing item gets its own clear `400`.
- Cast path params with `String(req.params.x)`. Parse query numbers with `parseInt(...) || default`.

**Auth**
- A route that acts on a tenant's behalf from the agent portal (sends messages or email, changes thread state) calls `requireInternalAgentApiKey(req, res)` as its first line. The guard returns `503` when `INTERNAL_API_KEY` is unset and `401` on a missing or wrong key.
- If the route uses multer, mount `requireInternalAgentApiKeyMiddleware` **before** the upload middleware, so an unauthenticated caller can't make the process buffer files.
- Compare any secret with `crypto.timingSafeEqual`, never `===`.

**Responses**
- Error body is always `{ error: string }`. Don't use the older `{ success: false, error }` variant in new code.
- Status codes: `400` invalid input · `401` bad key · `404` resource missing, or the feature isn't configured for this bot (e.g. `botInstance.emailDraftService` is undefined) · `409` conflict or already actioned · `503` config or dependency not available · `500` unexpected.
- Use numeric literals like the rest of `index.ts`. The `HTTP_STATUS_CODE` enum exists but nothing uses it, so don't mix styles.
- Always `return res...` so nothing runs after the response.
- Never return stack traces or raw provider errors. `err?.message` with a fallback string is the maximum.
- Paginated lists take `page` and `limit` query params (defaults 1 and 10) and return a pagination block `{ currentPage, totalPages, total…, hasNext, hasPrev }`. Copy `GET /api/user/:userId/threads`.

**Webhooks and long work**
- Meta-style webhooks ack `200` quickly. If work continues after `res.status(200)`, you cannot send another response, so every later failure must be logged with `Logger`.
- Webhooks from senders that retry (e.g. the email Logic App) must fail with non-2xx when processing fails, so the sender retries.
- Uploads use `multer.memoryStorage()` with explicit `limits` (`fileSize`, `files`).

**Logging**
- In `catch`, do **both** `console.error("[api/route] Error:", err)` and `Logger.error(...)` with a real `Modules` and `ERROR_CODE` value. See [[logging]].
- Don't `console.log(JSON.stringify(req.body))`. Bodies contain phone numbers, emails and message text.

## openapi.json — required in the same PR

Every added or changed route must be reflected in `openapi.json`:
- `operationId` (camelCase verb + noun, e.g. `pauseEmailThreadAi`), `summary`, `description`, `tags`.
- `security`: `[]` for open routes, `[{ "InternalApiKey": [] }]` for key-guarded ones.
- Every header, path and query parameter with `required`, `description` and an `example`.
- One response entry for **every** status code the handler can return. Errors reference `#/components/schemas/ErrorResponse`.
- New request or response bodies go in `components.schemas` and are referenced with `$ref`.

## Checklist
- [ ] Route sits beside its feature's routes in `src/index.ts`
- [ ] Guard added if agent-facing, and before multer when uploads are involved
- [ ] All inputs validated with `400 { error }`
- [ ] Uses `getOrCreateBotInstance(botId)`; logic lives in a service
- [ ] Every path returns exactly one response
- [ ] `catch` logs with `console.error` + `Logger.error(Modules.X, ERROR_CODE.X)`
- [ ] `openapi.json` updated
- [ ] New env vars added to `.env.example`
