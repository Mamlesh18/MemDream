---
name: multitenancy
description: Use when working with bot instances and tenant resolution — getOrCreateBotInstance, BotInstanceBundle, resolving botId from a webhook, clientId and tenant DB lookup, config invalidation via /api/reset and /api/config/callback, per-pod caches, or anything that must behave correctly for many bots in one process.
---

# Multi-tenancy: bots, clients and instances

One process serves many bots, for many clients. Almost every "works for one tenant, breaks for another" bug comes from something in this file being skipped.

Related: [[service]], [[data-access]], [[channel-adapter]], [[llm-and-prompts]].

## The three identifiers
- **botId** — one bot (one channel set, one config). Present on nearly every request.
- **clientId** — the tenant that owns the bot. Resolved from the config service, and names the tenant database.
- **socialAppId** — a provider-side id (Instagram/Facebook) used to find the botId when the route doesn't carry it.

Everything tenant-specific hangs off `DataConfigService`: `botId`, `clientId`, models, and all config getters. Never read tenant values from `process.env` — env is per-deployment, not per-tenant.

## Getting a bot instance

```ts
const botInstance = await getOrCreateBotInstance(botId);        // or (undefined, socialAppId)
```

- `botInstances: Map<botId, BotInstanceBundle>` is the cache. `botCreationPromises` de-duplicates concurrent creation, so two webhooks arriving together can't build two adapter sets. A failed creation removes the promise so a retry can work.
- Never call `initializeBot` directly, and never keep your own bundle cache. Handlers take what they need off the bundle each time.
- Adding something to the bundle: create it in `initializeBot`, return it, add the field to `BotInstanceBundle`, map it in `recreateBotFor`. See [[service]].
- Channel-conditional members are `| undefined` and the route answers `404 "… not configured for this bot"`.

## Tenant resolution

```
DataConfigService.init(socialAppId?)
  → main DB connection
  → configServiceHelper.getClientId()            (config service HTTP)
  → getClientConfig()                            (Redis-cached)
  → clientConfig.dataBaseName
  → client DB connection via useDb (shared pool)
```

Redis keys for each config are themselves fetched from the config service and memoized in a module-level map in `configServieHelper.ts`. Config reads therefore cost nothing after the first call — which is also why invalidation matters (below).

## Resolving botId from a webhook

| Channel | How |
|---|---|
| WhatsApp, RCS, Email, Bot Framework | `:botId` path parameter — always prefer this |
| Instagram / Facebook | Redis `instagram:appId:<id>` / `facebook:pageId:<id>`, else `clientConfig.botInfo` lookup, then `getOrCreateBotInstance(undefined, socialAppId)` |
| WhatsApp calls | `voiceOrchestrator` maps `callId → botId` |

When a bot instance can't be built, answer `503 { error: "Bot configuration not available", botId }` rather than a 500.

## Invalidation

| Trigger | Effect |
|---|---|
| `POST /api/reset` (`bot-id` header) | `refreshPlatformConfig()`, cleanup, remove from `botInstances` and `botCreationPromises`, recreate |
| `POST /api/config/callback` with `PLATFORM_CONFIG` | For **every** instance: `refreshPlatformConfig()` plus `resetClient()` on the image-extraction and speech services |
| `POST /api/config/callback` with any other config type | Drop and recreate that one bot's instance |

If you add a cached client or derived value built from config, it must be reset in one of those paths — `refreshPlatformConfig` for platform-level things (it also clears the derived system-prompt keys and resets the LLM singleton), the instance drop for bot-level things.

## Per-pod state

The service runs on several pods, and each holds its own `botInstances`.

- Caches that are safe per pod: immutable lookups like `botPhoneNumberIdMap` (botId → phoneNumberId).
- Caches that are **not** safe per pod: anything another request may need to see, or anything a config change must invalidate everywhere. Those go in Redis. See [[data-access]].
- Socket room sizes and the `clients` set are per pod. See [[chat-socket-events]].
- Polling transport needs sticky sessions (ARR affinity).

## Lifecycle notes
- `cleanupBotInstance(bundle, clearRedisKeysAlso)` is called on recreate and on shutdown. **Its body is currently empty.** If your new bundle member holds timers, sockets or provider connections, add explicit disposal there — otherwise a hot recreate leaks it.
- Graceful shutdown (`SIGTERM`/`SIGINT`) cleans every instance, shuts the ChatGateway down and closes the server, with a 10-second force-exit.
- There is a global `unhandledRejection` guard, but still attach `.catch` to every fire-and-forget promise. See [[error-handling]].

## Checklist
- [ ] Bot obtained via `getOrCreateBotInstance`; nothing cached privately
- [ ] All tenant data read through `DataConfigService`, not env
- [ ] New bundle members wired in all four places, and disposed if they hold resources
- [ ] New config-derived caches invalidated in `/api/reset` and `/api/config/callback`
- [ ] Nothing cross-request cached in module scope instead of Redis
- [ ] `503` when a tenant's config can't be loaded
