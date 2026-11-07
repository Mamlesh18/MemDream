---
name: channel-adapter
description: Use when adding a new messaging channel or changing an existing custom adapter in src/custom-adapters (WhatsApp Cloud API, Instagram, RCS Google, Email, or a new one) — inbound webhook parsing, outbound sendActivities, provider client, media handling, and registering the adapter in bot.ts and index.ts.
---

# Channel adapters

Each channel turns provider webhooks into Bot Framework `Activity` objects and sends replies back through the provider API.

Related: [[bot-middleware]], [[api-endpoint]], [[error-handling]].

## Folder structure

```
src/custom-adapters/<api-channel>/
  <Channel>Adapter.ts        # extends CustomWebAdapter — Activity <-> provider
  <Channel>Client.ts         # all HTTP calls to the provider API
  <Channel>MultiMediaTypes.ts # provider payload types (webhook + send shapes)
  index.ts                   # barrel: export * from each file
```

Examples: `api-instagram/`, `api-rcsgoogle/`, `cloudapi-whatsapp/`. `email/` is the newest and most carefully commented.

## Adapter

```ts
export class ThingAdapter extends CustomWebAdapter {          // @botbuildercommunity/core
  protected readonly settings: ThingAdapterSettings;
  protected readonly channel: string = 'THING';               // uppercase channel id
  public client: ThingClient;

  constructor(botId: string, clientId: string, settings: ThingAdapterSettings,
              telemetryClient: BotTelemetryClient) { ... }

  sendActivities(context, activities)   // switch on activity.type:
                                        //   Message → parseActivity → client.send; 'delay' → wait;
                                        //   default → console.warn + push {} (never throw)
  updateActivity() / deleteActivity()   // throw new Error('<Channel> API does not support …')
  continueConversation(reference, logic) // applyConversationReference → createContext → runMiddleware
  processActivity(req, res, logic)      // verify → parse → build Activity → runMiddleware
}
```

## Client
- Owns the access token and base URL, and makes **every** provider HTTP call with `axios`. Don't use the old `request` library in new code.
- Each call: try → check the status → return data. On failure: `Logger.error(err, '<method>', '<description>', Modules.<CHANNEL>_CLIENT, ERROR_CODE.SEND_MESSAGE_FAILED)` → rethrow.
- Pin the API version in one field (`this.graphUrl = 'https://graph.facebook.com/v19.0'`).

## Inbound rules
- **Verify first.** Check the shared secret or signature with `timingSafeEqual` before parsing, like `EmailAdapter.validateInboundSecret`. Webhooks with a GET verification handshake need a GET route too.
- **Choose the ack strategy on purpose and write it in a comment:**
  - *Provider doesn't retry usefully (Meta):* ack `200` early, then process. Ignore echoes (`is_echo`, self-sent) and dedupe by provider message id.
  - *Provider retries and loss matters (email Logic App):* ack only after persistence succeeds. The adapter's `onTurnError` **rethrows** so the webhook fails and the sender retries. Swallow deterministic denials (`ConversationLimitExceededError`) so they aren't retried forever.
- Map provider fields to `Activity` consistently: `from.id` = channel user id, `recipient` = bot identity, `conversation.id` = stable per user or thread, `channelId` = the uppercase channel, `channelData` = raw provider payload for later middleware.
- **Media:** download from the provider, upload with `uploadToAzure` (`src/helpers/blobImageUrl.ts`), and put the blob URL in `activity.attachments`. Never pass provider media URLs downstream, since they expire and need auth.
- botId resolution: prefer a `:botId` route param. Social channels can fall back to a Redis lookup (`instagram:appId:<id>`) and `getOrCreateBotInstance(undefined, socialAppId)`.

## Registration

1. **`src/bot.ts` → `initializeBot()`**
   - Read the channel's config from `channelConfig.channels` (lowercase name match).
   - Construct the adapter **only if the required config is present**. Otherwise leave it `undefined`.
   - Set `adapter.onTurnError`: the shared `onTurnErrorHandler`, or a custom one for retrying channels (see above).
   - Register middleware **in this order**: `feedbackMiddleware` → `userMiddleware` → (channel typing) → `loggerMiddleware` → (channel-specific gates, e.g. `AiPauseMiddleware`) → `dailyUserLimitMiddleware`. See [[bot-middleware]].
   - Add the adapter to the `ConversationHelper` adapter map with a new `ADAPTERS` enum key, so proactive messages work.
   - Add it to the `return { ... }`.
2. **`src/index.ts`**
   - Add the field to `BotInstanceBundle` (`<Channel>Adapter | undefined`) and map it in `recreateBotFor()`.
   - Add the webhook route(s) next to the other channel webhooks. Return `404`/`503` when the bot has no such adapter. See [[api-endpoint]].
3. **`src/enum/index.ts`**: add `Modules.<CHANNEL>_ADAPTER`, `Modules.<CHANNEL>_CLIENT`, a new `ERROR_CODE` block, and an `ADAPTERS` key.
4. **`openapi.json`**: document the webhook(s) and any auth header.
5. **`.env.example`** for any new env vars. **README.md** channel table.

## Checklist
- [ ] Adapter / Client / MultiMediaTypes / index.ts structure
- [ ] Inbound verified with constant-time compare
- [ ] Ack strategy chosen and commented; echoes and duplicates ignored
- [ ] Media re-hosted in Azure Blob
- [ ] Constructed only when configured; `onTurnError` set; middleware order correct
- [ ] Wired into ConversationHelper, return object, BotInstanceBundle, recreateBotFor
- [ ] Enums, openapi.json, .env.example, README updated
