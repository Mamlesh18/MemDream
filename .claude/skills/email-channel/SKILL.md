---
name: email-channel
description: Use when working on the email channel — the inbound email webhook and its dedup/self-send/relevance guards, AI draft replies and the approve/reject flow, thread pause/resume, outbound sending through the tenant Logic App, email attachments, or email message status.
---

# Email channel

The email channel has more correctness guards than any other channel, because email **retries** and a duplicate reply is visible to the customer.

Related: [[channel-adapter]], [[bot-middleware]], [[data-access]], [[llm-and-prompts]].

## Files

| Concern | File |
|---|---|
| Adapter: inbound parse, outbound send, provider resolution | `src/custom-adapters/email/EmailAdapter.ts` |
| Webhook and its guards | `POST /api/email/messages/:botId` in `src/index.ts` |
| Draft lifecycle | `src/services/emailDraftService.ts`, `message.service.ts` |
| Multi-mailbox dedup | `src/services/emailDedupService.ts` |
| Relevance classification | `src/services/emailClassificationService.ts` |
| Logic App send | `src/services/outboundEmailService.ts` |
| Agent routes (approve, reject, compose, pause, resume) | `src/index.ts` |
| Pause gate | `src/middleware/aiPause.middleware.ts` |

Design reference: `ARCH-LLD-001-Email-Channel.md` (cited throughout the code).

## Inbound order — do not reorder

```
botId present            → 400
adapter configured       → 404
x-email-shared-secret    → 401     (constant-time compare)
metadata-only log        (never the body, subject or addresses)
guard 1: isSelfSentEmail        → 200, skip   (bot's own reply returning via its Cc)
guard 2: shouldDeferToBotMailbox → 200, skip  (agent mailbox copy of a bot-mailbox email)
dedup claim on internetMessageId → 200, skip when already claimed
relevance classification         → 200, skip when not relevant (fails open)
processActivity → bot.run  → 200 only after persistence and bot run succeed
on failure: releaseClaim + non-200 so the Logic App retries
```

Why the order matters:
- **Both guards come before the claim.** The claim key is shared by every mailbox copy, so a copy that is about to be skipped must not take it — that would block the copy that should have been processed.
- **Dedup and classification come before any persistence**, so a skipped email is never saved.
- Every skip answers **200**, otherwise the Logic App retries an email we deliberately dropped.

## Dedup
- Key is `internetMessageId` (RFC5322 Message-ID) — the one value identical across mailbox copies. Graph's `id` is mailbox-local and must not be used.
- The claim is a unique-index `create()`, atomic across pods. Duplicate key (`11000`) means "already claimed". Never check-then-insert.
- `buildClaimKey` falls back to `conversationId:messageId`, and returns `undefined` when there's no identifier at all — that means **fail open** and process it. A missed dedup is cheaper than silently dropping real mail.
- An infra error in `claimMessage` also fails open (returns `true`).
- `releaseClaim` on failure is what makes delivery at-least-once. Never remove it.

## Draft lifecycle

```
AgentIntelligentDialog sets value.draft = true
  → LoggerMiddleware.onSendActivities persists status DRAFT and strips it from the adapter batch
  → onDraftCreated: emit `emailDraftPending` + create EMAIL_DRAFT workflow timeout
  → approve: claimDraftForSending (DRAFT → SENDING, atomic) → send → markMessageSent / approveDraft (matches SENDING)
  → reject: rejectDraft
```

- **Never auto-send.** A draft always waits for a human.
- `SENDING` is a claim, not a progress flag. Two concurrent approvals, or an approve racing a reject, are resolved by that one conditional update. The loser gets `409`.
- Failure **before** the send releases the claim (`releaseDraftClaim`). Failure **after** the send deliberately leaves it `SENDING` for manual reconciliation — reverting would invite a retry that mails the customer twice.
- Draft timeout comes from `EMAIL_DRAFT_TIMEOUT_SECONDS` (default 30 minutes) and escalates; a failed timer must never block the draft from being reviewable.
- `Message.status` is set for **every** email: inbound is `SENT`, held replies are `DRAFT`. Don't stamp status on other channels — it collides with their delivery-status values.

## Outbound
- Resolve the provider **at send time**, not at draft time: `resolveEmailProvider(config, agentId, receivedByAddress)` picks the approving agent's mailbox, falling back to the bot's default. Tenants rotate Logic App URLs.
- Every reply Cc's the bot mailbox (`computeReplyCc`) so it keeps a copy of the thread — which is exactly why guard 1 exists.
- Bodies are rendered with `markdownToEmailHtml`. Quoted chains are stripped with `cleanEmailBody` before anything is sent to an LLM.
- Attachments stay **in memory** (multer `memoryStorage`), are base64'd for the Logic App and discarded. Raw bytes are never persisted and there is no URL-fetch path — keep it that way, it's what keeps SSRF out.

## Pause / resume
- `threadService.setPaused(threadId, bool)` writes both `isPaused` (durable) and `userState = PAUSED`.
- `AiPauseMiddleware` runs **after** `LoggerMiddleware`, so a paused thread still persists the inbound email but never reaches the dialog or AI. Don't move it earlier.

## Errors
- `Modules.EMAIL_ADAPTER` / `EMAIL_DRAFT_SERVICE`, error codes `INL2700–`.
- The email adapter's `onTurnError` **rethrows** so the webhook fails and the sender retries. `ConversationLimitExceededError` is swallowed, because retrying a deterministic denial is pointless. See [[error-handling]].
- Agent routes need `x-internal-api-key`, mounted before multer. See [[api-endpoint]].

## Never log
Email bodies, subjects, addresses, attachment bytes, or classification input. This handler is deliberately metadata-only everywhere, including failure paths. See [[logging]].

## Checklist
- [ ] Guard order unchanged; every skip returns 200
- [ ] Claim is atomic and released on failure
- [ ] No auto-send; `SENDING` claim respected on both failure sides
- [ ] Provider resolved at send time; bot mailbox Cc'd
- [ ] Attachments never persisted raw
- [ ] `AiPauseMiddleware` still after `LoggerMiddleware`
- [ ] No email content in logs
