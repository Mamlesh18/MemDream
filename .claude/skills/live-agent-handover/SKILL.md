---
name: live-agent-handover
description: Use when working on live agent handover — queueing a user to an agent, agent availability and working hours, Area of Speciality (AOS) routing, USER_STATE transitions (BOT/QUEUED/AGENT/CO_EXISTENCE), the /api/handOver and /api/agent endpoints, LiveAgentDialog, or agent waiting timeouts.
---

# Live agent handover

Related: [[chat-socket-events]], [[dialog]], [[api-endpoint]], [[data-access]].

## Files

| Concern | File |
|---|---|
| Queueing, availability, co-existence, `manageUser` | `src/services/liveagent.service.ts` |
| In-chat "talk to an agent" flow | `src/dialogs/liveAgent.dialog.ts` |
| Portal asks to queue a user | `POST /api/handOver` in `src/index.ts` |
| Agent sends a message to a customer | `POST /api/agent` in `src/index.ts` |
| Waiting timers | `src/services/workflowtimeout.service.ts`, `POST /api/session/timeout` |
| Portal notification | `chatGateway.handleUserRequested` |

Configuration comes from the **agent-assist skill** in `skillConfig`: `liveAgentWaitingTimeout` (minutes, default 15), `coExistenceTimeout`, `timeZone`, `availability.workingHours`, `availability.message`.

## Thread state machine

`UserThread.userState` is the source of truth, not the contact or the user document.

```
BOT ──queueForAgent──► QUEUED ──agent replies──► AGENT
 ▲                        │                        │
 └──── unqueueForAgent ◄──┴────────────────────────┘   (user types "bot", timeout, agent leaves)

CO_EXISTENCE  ← WhatsApp `smb_message_echoes` (business replied from their own WhatsApp app)
CALL_*        ← see whatsapp-calling
```

Rules:
- Change state only through `LiveAgentService` / `ThreadService`. Never write `userState` from a route handler.
- Every transition also updates `aosId` (set it or clear it to `null`) and the workflow timeout.
- `queueForAgent` is **idempotent**: if the thread is already `QUEUED` it only refreshes `aosId` and returns. Keep that early return.
- Leaving a state must delete the workflow timeout (`deleteWorkflowTimeout(threadId)`), otherwise a stale timer fires later.

## Availability
- `getAgentAvailability()` reads working hours in the tenant's timezone with `moment-timezone`, parses `"hh:mm A"` times, and handles windows crossing midnight.
- On error it returns `{ online: false }` — **fail closed**. Don't change that: queueing a customer when no agent will answer is worse than telling them the hours.
- `POST /api/handOver` answers **202** (not 4xx) with `reason: "AGENT_OFFLINE"` plus `message`, `timeZone`, `availabilityTime` and `workingHours`. Offline is a normal outcome, so keep it a success response.

## Queueing needs a TurnContext

`queueForAgent(activity, turnContext)` needs middleware-populated `userData` and state accessors. An HTTP route cannot invent that, so `/api/handOver`:

1. Looks up the `ChannelSession` by `activity.conversation.id`.
2. Reads its stored `conversationReference`, and returns `404` when it's missing.
3. Rewrites `activity.conversation.id` and `activity.recipient.id` from the session.
4. Calls `conversationHelper.continueConversation(reference, ctx => liveAgentService.queueForAgent(activity, ctx))`.

Any new route that has to act as the user inside a turn follows this same sequence.

## AOS routing (Area of Speciality)

`activity.value.aosId` has three meanings, and they are different:

| Value | Meaning |
|---|---|
| `undefined` | Infer it: `resolveAosId` sends the last 10 messages plus the ACTIVE AOS titles to `LlmService.detectAosId` |
| `null` or `""` | No speciality — route bot-wide |
| an id | Use it, **after** checking that AOS is `ACTIVE` |

- An unknown or inactive `aosId` falls back to bot-wide routing with a log line. Never queue into a room nobody watches.
- The LLM only sees titles and descriptions; the caller maps the title back to the id. Keep it that way — don't ask the model to emit opaque ids.
- `specialists[].specialistId` (emails) are the FCM notification targets when an AOS matched.

## Notifying agents
- `chatGateway.handleUserRequested({ room: botId, conversationId, aosId })` emits `userRequested` to `aosId:<id>` when there's an AOS, otherwise `bot:<botId>`. See [[chat-socket-events]].
- FCM calling notification is **best-effort**: fire it with `.catch(...)` and never let it block or fail the queueing.
- The "User request for an agent" transcript event is also fire-and-forget through `loggerMiddleware.insert(...).catch(...)`.

## `/api/agent` (agent → customer)
- Logs **shape only**, never the body. It carries customer messages and base64 attachments.
- Base64 attachments are bounded (count, per-file bytes, 50 MB total) **before** decoding, uploaded once with `uploadToAzure`, and `contentBase64` is stripped so raw bytes are never persisted. Keep every bound if you touch this.
- An enforcement denial reported through `turnState` becomes a `429`. See [[usage-enforcement]].

## Errors and logging
- `Modules.LIVE_AGENT_SERVICE` with `QUEUE_FOR_AGENT_FAILED`, `UNQUEUE_FOR_AGENT_FAILED`, `CHECK_AGENT_ONLINE_FAILED`, `UPDATE_CONTACT_FAILED`.
- Include `threadId`, `conversationId` and `aosId` in log details. Never log message text. See [[logging]].

## Checklist
- [ ] State changed through the service, with `aosId` and the timeout kept in step
- [ ] `queueForAgent` stays idempotent for an already-queued thread
- [ ] Availability failure still means offline
- [ ] `aosId` validated as ACTIVE, with a bot-wide fallback
- [ ] Agent notified through the right room; FCM stays best-effort
- [ ] No customer message content in logs
