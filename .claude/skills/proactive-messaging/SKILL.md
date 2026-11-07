---
name: proactive-messaging
description: Use when sending a message to a user outside a normal turn — continueConversation, conversation references from channel sessions, the proActiveMessage / notify-user / session-timeout / integrations endpoints, workflow timeouts, or messages sent outside WhatsApp's 24-hour window.
---

# Proactive messaging (sending outside a turn)

Related: [[api-endpoint]], [[whatsapp-templates]], [[live-agent-handover]], [[usage-enforcement]].

## Always go through ConversationHelper

```ts
await botInstance.conversationHelper.continueConversation(reference, async (proactiveContext) => {
  // a full turn: middleware ran, turnState has userData, accessors work
  await proactiveContext.sendActivity(activity);
});
```

`helpers/ConversationHelper.ts` picks the adapter:
- `web` / `directline` / `facebook` / `emulator` / `webchat` → `defaultAdapter.continueConversationAsync(AZURE_BOT_APPLICATION_ID, …)`
- everything else → `adapters[ADAPTERS[channelId]].continueConversation(…)`

A new channel therefore needs an `ADAPTERS` key **and** an entry in the ConversationHelper map, or proactive sending throws. See [[channel-adapter]].

**Enforcement can silently suppress your callback.** If `UserMiddleware` puts `enforcementDenied` in `turnState`, the wrapped callback returns without running, unless the caller passes `{ surfaceDenial: true }` (only `/api/agent` does, to answer 429). So never assume the callback ran, and never report success based on `continueConversation` resolving. See [[usage-enforcement]].

## Getting a conversation reference

Never hand-build a reference. Look up the channel session and use its stored `conversationReference`:

| You have | Lookup |
|---|---|
| `channelSessionId` | `channelSessionService.findChannelSession({ channelSessionId })` |
| A channel conversation id | `findChannelSession({ channelConversationId })` |
| `threadId` | `findChannelSessionsByThreadId(threadId)` |

Missing session or reference → `404`, with a clear message. Don't fall back to guessing ids.

## Inside the callback
- Read the language from `UserProfile` via `userState.createProperty<UserProfile>(GENERAL.USER_PROFILE_PROPERTY)`, then build text with `getDynamicContent(lgTemplate, preferredLanguage, section, key)`. See [[dialog]].
- `userData` (user, contact, thread, channelSession) is in `turnState` — use it instead of new queries.
- Set `activity.from = { role: 'bot' }` and align `activity.conversationId` with the reference.
- State is **not** auto-saved here the way `DialogBot` saves it in a normal turn. If you change state, call `saveChanges` explicitly. See [[bot-state]].

## The endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/proActiveMessage/:botId` | Timer-driven events (e.g. `LIVE_AGENT_NOT_JOINED` → unqueue + "agents unavailable") |
| `POST /api/session/timeout` | Workflow timeout fired: picks the LG message by `type` (`AGENT`, `SKILL`, `CO_EXISTENCE`) and the thread's `userState`, then resets state |
| `POST /api/notify-user` | Bulk notifications: answers `200` **first**, then loops over `notifications[]` |
| `POST /api/integrations/sendProactiveMessage` | Integration-driven message by `intentValue` + `lookUpKey` + `placeHolder` |
| `POST /api/integrations/outcomes` | Outcome callbacks (e.g. payment success) that resume a skill's conversation |

`/api/notify-user` responds before it sends, so **nothing after that line can report an error to the caller** — every failure in the loop must be logged. See [[logging]].

## Workflow timeouts

`WorkflowTimeoutService` writes rows an external timer service polls; when one expires it calls `/api/session/timeout`.

- `createWorkflowTimeout({ clientId, botId, threadId, type, timeoutInSec, lastAccessedAt })`
- `updateLastaccessedWorkflowTimeout(threadId)` to extend
- `deleteWorkflowTimeout(threadId, type?)` when the workflow ends

**Careful:** `createWorkflowTimeout` first deletes **any** existing row for that `threadId`, regardless of type. One thread holds one timer, so creating an `EMAIL_DRAFT` timer replaces a pending `AGENT` timer. Check for an existing timer before adding a new type to a flow.

Types in use: `AGENT`, `SKILL`, `CO_EXISTENCE`, `EMAIL_DRAFT`.

## The 24-hour window
- Outside WhatsApp's 24-hour customer-service window, only an approved **template** can be delivered. `/api/notify-user` supports this with `isTemplateMessage`, compiling `attachments` and `contentData` through Handlebars with the notification's `parameter` values.
- Sending a plain text message outside the window fails at Meta, not in this code. See [[whatsapp-templates]].
- A template send to a brand-new user may also be a Campaign Entry. See [[broadcast-and-campaign]].

## Checklist
- [ ] Sends through `conversationHelper.continueConversation`, never an adapter directly
- [ ] Reference comes from a channel session; `404` when absent
- [ ] No assumption that the callback ran (enforcement may suppress it)
- [ ] Text from the LG template with the user's language
- [ ] State saved explicitly if changed
- [ ] Failures after an early `200` are logged
- [ ] Timer created, extended and deleted, with the single-timer-per-thread rule in mind
