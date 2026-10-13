---
name: notifications
description: Use when working on agent notifications — agent mentions and the /api/mentions endpoints, multi-channel fan-out to portal, email, WhatsApp and FCM, per-agent notification preferences, Firebase setup, or FCM token handling.
---

# Agent notifications

Notifying **agents** (not customers). Customer messaging is [[proactive-messaging]].

Related: [[chat-socket-events]], [[whatsapp-templates]], [[live-agent-handover]].

## Files
- `src/services/mention-notification.service.ts` — mention fan-out across channels
- `src/services/firebase.service.ts` — Firebase Admin init and FCM send
- `UserService.sendFirebaseCallingNotification` — FCM for live-agent requests and calls
- `POST /api/mentions`, `POST /api/mentions-reply` in `src/index.ts`
- Portal realtime: `chatGateway.handleMentionDiscussion` → `discussion` event

## Mention endpoints
- Validation collects **all** problems into an `errors[]` array and returns one `400`, instead of failing on the first. Keep that shape — the portal renders the list.
- Required: `userId`, `messageId`, `threadId`, `contactId`, `from.id` (must be an email), `from.name`, a non-empty `recipient[]` (each with `id` and `name`), and `activity.text` **or** `activity.attachments`.
- `bot-id` header identifies the tenant.

## Fan-out rules

`sendAgentMentionNotifications(mention)`:

1. Find the recipient in `ClientUser` by `{ email: recipient.id, 'bot.botId': botId }`. Not found → `trackEvent('RecipientAgentNotFound')` and return. This is **not** an error — the agent may have been removed.
2. Read that bot's entry's preferences: `notifications.mentions` or `notifications.agentMention`. `enabled !== true` → `trackEvent` and return.
3. `channels[]` is lowercased and trimmed, then matched: `portal`/`web`, `email`, `whatsapp`.
4. All sends run under `Promise.allSettled` — one channel failing never blocks the others.

Rules:
- **Always respect per-agent preferences.** Don't add a channel that ignores them.
- **Never notify agents of another bot or tenant.** Every lookup is scoped by `botId` (and `clientId` for tokens).
- Use `trackEvent` for expected, configuration-caused outcomes (disabled, no recipient, no template) and `logger.error` only for real failures. See [[logging]].
- Notifications are best-effort: never let them fail the action that triggered them.

## WhatsApp notification channel
Loads the configured approved template, interpolates variables deeply, then normalizes it for sending. A missing or unusable template is `trackEvent('WhatsAppTemplateLoadFailed')` plus `null` — the channel is skipped, others still go. Template names come from config, never hardcoded. See [[whatsapp-templates]].

## FCM
- `initializeFirebase()` is lazy and idempotent, reading `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY` (with `\n` unescaped) and `FIREBASE_CLIENT_EMAIL`. Missing vars → returns `null`, and the feature is simply off. Don't throw on a deployment without Firebase.
- `sendFcmNotification(token, data)` sends a **data-only** message. Keys and values are strings.
- Token targeting (`sendFirebaseCallingNotification`): `FcmToken` by `{ clientId, environment: 'AGENT-ASSIST' }` → narrow to `ACTIVE` `ClientUser`s assigned to this `botId` → build an email → tokens map → send. An AOS match narrows further to that speciality's specialist emails.
- **Stale tokens:** on an error whose `code` starts with `messaging/`, delete the token row. Keep this, or dead devices are retried forever.
- Per-token failures are logged with `FIREBASE_CALLING_NOTIFICATION_FAILED` and don't stop the other sends.

## Never log
Mention text, customer messages, agent emails in message strings, or FCM payload contents. Log ids and counts. See [[logging]].

## Checklist
- [ ] All validation errors returned together as `400` with `errors[]`
- [ ] Recipient resolved and scoped to this bot; preferences respected
- [ ] `Promise.allSettled` so one channel can't block others
- [ ] Missing config → `trackEvent` and skip, not an error
- [ ] Invalid FCM tokens deleted
- [ ] Nothing here can fail the triggering action
