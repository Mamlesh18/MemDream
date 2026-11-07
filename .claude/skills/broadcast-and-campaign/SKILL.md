---
name: broadcast-and-campaign
description: Use when working on broadcast replies (a customer replying to a broadcast template, session seeding, the broadcast report table, template rebuilding) or Campaign Entry outcomes from Meta ad referrals and first outbound broadcast messages.
---

# Broadcasts and campaign entries

Broadcasts are **sent by another repo** (`inflow-queue-processor-repo` / `inflow-broadcast-v6`). This repo handles two things: what happens when someone **replies** to a broadcast, and the **Campaign Entry** outcome.

Related: [[whatsapp-templates]], [[data-access]], [[multitenancy]].

## Broadcast replies

`src/services/broadcast-reply.service.ts`, entered from `seedBroadcastReplySession()` in `src/index.ts` before the WhatsApp turn runs.

### The gates, in order
1. `messages[0].context.id` present — only a **reply** can reference a broadcast.
2. `context.from === metadata.display_phone_number` — it must be a reply to a message *we* sent.
3. `contacts[0].wa_id` present.
4. `hasExistingChannelSession(wa_id)` is false — an existing session means the normal flow already has what it needs.
5. `findBroadcastByMessageId(botId, context.id)` returns a row — `null` is the **normal** case (an ordinary reply), not an error.

`hasExistingChannelSession` fails **closed** (returns `true` on error), so a DB blip creates no duplicate seeded session.

### The report table
- Azure Table, name from `REPORT_TABLE_NAME` (default `broadcastReport`). `PartitionKey` = jobId, `RowKey` = `${messageId}-${status}`.
- A message id is a **RowKey prefix range**, not a point read: `>= "{id}-"` and `< "{id}."` (because `-` is 0x2D and `.` is 0x2E).
- One message has a row per status; only the row written by the broadcast timer carries `placeholders`, so prefer a row that has them and keep the first row as a fallback.

### Seeding a session
`seedSessionFromBroadcast` creates the channel session (and with it conversation, contact, user, thread), then persists the original broadcast as the thread's first message so the agent sees what the customer is replying to.

- **Session keys must match what `ContactService.findOrCreateUser` queries on the following turn** (`botId`, `channelUserId`, `channelConversationId` — both the `wa_id` for WhatsApp). A mismatch creates a second session and orphans the seeded one.
- Persisting the original message is supplementary: it is deduped by `messageId` and must never fail the inbound turn.

### Rebuilding the template
`rebuildTemplate(template, placeholders)` substitutes the recipient's values back into the stored template. **The traversal order is a cross-repo contract** with `AzureHelpers.ExtractPlaceholderValues` in `inflow-broadcast-v6`:

- components in order, then parameters in order
- a text parameter consumes one slot
- a media link consumes one slot
- a button **payload** parameter consumes **none**

Changing the order here without changing the producer silently shifts every placeholder. The media *type* comes from the `BroadcastJobs` template (`findBroadcastJobTemplate`), because the placeholder array only holds the link.

## Campaign Entry outcomes

`src/services/campaignEntry.service.ts` — two origins, one outcome:

| Method | Fires when |
|---|---|
| `recordCampaignEntryIfApplicable` | A new session starts from a Meta ad referral (Click-to-WhatsApp / Click-to-Instagram) |
| `recordBroadcastCampaignEntryIfApplicable` | The first outbound broadcast template opens a conversation with a brand-new user (only from `/api/agent`'s new-session branch) |

Rules:
- Gated per bot by `botConfig.campaignEntryOutcome.enabled`, and deduped to **once per new session** (`isNewConversation`).
- `conversationId` in the payload **must be `channelSession.channelSessionId`**, not the raw WhatsApp/Instagram conversation id. Downstream depends on it.
- `context.turnState.set('responseId', payload.responseId)` must happen **before any `await`**, because `LoggerMiddleware` reads it when it persists this turn's messages and writes the back-reference.
- `channel` is the real originating channel, never hardcoded.
- **Never block the turn.** Everything is wrapped in try/catch and logged with `CAMPAIGN_ENTRY_OUTCOME_FAILED`.

## Azure Queue
`AzureQueueService` (`QueueList.RESPONSE_QUEUE`): `getQueueMessage(queueName)` returns a client and calls `createIfNotExists()` first. Payloads are `JSON.stringify` → base64. Credentials come from `AZURE_STORAGE_ACCOUNT_NAME` / `_KEY`.

## Checklist
- [ ] Reply gates kept in order; a non-broadcast reply exits quietly
- [ ] Seeded session keys match `findOrCreateUser`'s query
- [ ] Placeholder traversal still matches the broadcast repo
- [ ] Campaign Entry uses `channelSessionId`, sets `responseId` before awaits
- [ ] Nothing here can fail the customer's turn
