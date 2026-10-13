---
name: data-access
description: Use when reading or writing MongoDB (Mongoose models, queries, upserts, new collections or fields) or Redis (keys, TTLs, locks, cross-pod state), or when reading tenant config (clientConfig, botConfig, channelConfig, skillConfig, lgTemplate) in this repo.
---

# Data access: MongoDB and Redis

Related: [[service]], [[error-handling]].

## MongoDB

### Where models come from
- Schemas and model factories are **not defined in this repo**. They come from the `@inextlabs/schema-models` package.
- A new collection or field means: change `schema-models` → publish → bump its version in `package.json` → (new collection only) add a getter to `DataConfigService`.
- Always get models through `DataConfigService` getters, e.g. `dataConfigService.getUserThreadModel()`. Never call a model factory or `mongoose.model` from a service.

### Two databases
| Connection | Holds | Example getters |
|---|---|---|
| `mainDbConnection` (platform) | Cross-tenant platform data | `getWorkflowTimeoutModel()` |
| `clientDbConnection` (tenant DB named in `ClientConfig.dataBaseName`) | Everything tenant-owned | `getUserModel()`, `getMessageModel()`, `getUserThreadModel()`, `getCallHistoryModel()` |

- All connections share **one pool** (`src/database/database.service.ts`, `useDb()` per tenant). Never call `mongoose.connect` or `createConnection` anywhere else.
- A new getter follows the existing one-liner style:
  ```ts
  public getThingModel(): Model<ThingDocument> {
    return thingModel(this.clientDbConnection);
  }
  ```

### Query rules
- **Every query is scoped by `botId`**: `{ botId: this.dataConfigService.botId, ... }`. Tenant DBs can hold several bots.
- Business ids (`threadId`, `messageId`, `callId`) are what APIs expose. Don't expose Mongo `_id`.
- Reads that don't need a document instance use `.lean()`.
- Paginate with `limit` / `skip` plus `countDocuments` using the same filter.

### Write rules
- Prefer **atomic** `findOneAndUpdate(filter, update, { new: true })` over find → modify → `save()`.
- **State transitions and claims** put the expected current state in the filter, so two concurrent requests can't both win:
  ```ts
  // DRAFT → SENDING claim: only one approver gets a document back
  await model.findOneAndUpdate(
    { botId, messageId, status: EMAIL_DRAFT_STATUS.DRAFT },
    { $set: { status: EMAIL_DRAFT_STATUS.SENDING } },
    { new: true }
  );
  ```
- **Upserts**: immutable identity fields go in `$setOnInsert`, mutable fields in `$set`. The same path, or a parent and child path (`recipient` and `recipient.id`), must **never** appear in both. MongoDB rejects that. See `callhistory.service.ts`.
- Never write `null` or `undefined` for an optional field by accident. Use a spread guard: `...(reason && { callRejectedReason: reason })`.

### Enums that exist twice
- `CHANNEL_ID` in `src/enum` is out of date. For values stored in or compared against the DB, use `CHANNEL_ID` from `@inextlabs/schema-models`. For channel config lookups, compare lowercase names: `channels.find(ch => ch.name.toLowerCase() === 'email')`.
- `USER_STATE` is duplicated by hand in `src/enum`. When you add a state, add it in `schema-models` too, with the same value.

### Tenant config
- Read config **only** through `DataConfigService`: `getClientConfig()`, `getBotConfig()`, `getChannelConfig()`, `getSkillConfig()`, `getLgTemplate()`, `getAgentConfig()`, `getTemplateConfig()`. Platform-level: `getPlatformConfigByName(name)`, `getModelMasterById(id)`, `getModelMasterByType(type)`.
- Each getter reads a Redis key and falls back to the config service over HTTP (`CONFIG_SERVICE_BASE_URL`) when the key is cold. The **Redis key names themselves** come from the config service and are memoized in a module-level map in `helpers/configServieHelper.ts`, so config reads are nearly free after the first one.
- Never query config collections directly, never write config from here, and never cache a derived value from config without invalidating it — see the invalidation table in [[multitenancy]].
- Config is per tenant. Anything in `process.env` is per deployment, so it can never hold tenant settings.

## Redis

### Client
- One client from `initializeRedis()` (`src/redis/redis.service.ts`), created at start-up and passed down. Never `createClient` elsewhere.

### Keys
- Format: `<domain>:<entity>:<id>`, lowercase, colon-separated.
  Existing: `call:meta:${callId}`, `call:flags:${callId}`, `call:phone:${phone}`, `instagram:appId:${id}`, `facebook:pageId:${id}`.
- Build keys in **one** private helper in the owning file (see the key functions in `src/redis/call-state.redis.ts`). Don't scatter string templates.
- Values are `JSON.stringify` on write and `JSON.parse` on read, with a `null` check.

### Rules
- Ephemeral state **always** has a TTL (`EX`). Clean up explicitly when the flow ends, and keep the TTL as a safety net.
- Locks, dedup and "only once" guards must be **atomic**: `SET key value NX EX ttl`, or a Lua script via `eval` for compare-and-swap. Never GET then SET (see `NX_SET_SCRIPT` and `CAS_SUMMARY_SCRIPT` in `call-state.redis.ts`).
- The app runs on **multiple pods**. Anything another request might need (call state, dedup, locks) goes in Redis, not a module-level `Map`. In-memory maps are only for things tied to one process (timers, live WebRTC objects).
- A new domain's Redis logic goes in `src/redis/<domain>.redis.ts` as a class that takes `RedisClientType`, like `CallStateRedis`.

## Checklist
- [ ] Model obtained via a `DataConfigService` getter; new models added to `schema-models` first
- [ ] Query filtered by `botId`
- [ ] Writes atomic; state transitions guarded by current state in the filter
- [ ] No path conflict between `$set` and `$setOnInsert`; no accidental null writes
- [ ] Redis keys follow `domain:entity:id`, have a TTL, built in one helper
- [ ] Locks and dedup use NX or Lua, never GET-then-SET
