---
name: debug-production
description: Use when investigating a live or reported issue — finding logs and exceptions in Application Insights with KQL, Elastic APM traces, reading the /health endpoint, and first checks for common incidents (bot silent, portal missing events, email not replying, stale config, no call audio).
---

# Debugging a live issue

Related: [[logging]], [[multitenancy]], [[build-and-deploy]].

## Where to look

| Tool | What it gives |
|---|---|
| **Application Insights** | `traces` (Logger.log), `exceptions` (Logger.error), `customEvents` (trackEvent). Every entry carries `botId`, `clientId`, `environment`, `service`, and errors add `errorTitle`, `errorDescription`, `errorCode`, `module`, `errorMessage`, `errorStack` |
| **Elastic APM** | Transactions named `Class.method` on the message path (thread, user, conversation, channelSession, adapters) |
| **`GET /health`** | `applicationStartTime` (spot restarts), `botInstances` (which tenants this pod has built), and listener counts vs max with a usage % — that block exists because listener leaks have happened |
| **Container logs** | `console.*` output, JSON-wrapped in production |
| **`azureDashboard.kql`** | The dashboard queries |

## Useful queries

```kusto
// every error for one tenant, newest first
exceptions
| where customDimensions.botId == "<BOT_ID>"
| where timestamp > ago(1h)
| project timestamp, customDimensions.errorCode, customDimensions.errorTitle,
          customDimensions.errorDescription, customDimensions.module, customDimensions.errorMessage
| order by timestamp desc

// one feature, by error-code range (see error-handling for the ranges)
exceptions | where tostring(customDimensions.errorCode) startswith "INL26"   // WhatsApp calls

// one conversation or call
traces | where customDimensions.threadId == "<THREAD_ID>"   // or callId / messageId
```

Two caveats:
- **`correlationId` in logs is generated per log call**, not per request. It does not stitch a request together. Correlate on `threadId`, `callId`, `messageId` or `conversationId` instead. (The WhatsApp webhook does accept an `x-correlation-id` / `x-request-id` header and logs it in its own console lines.)
- Fields whose **key** contains name/phone/email/address are masked in telemetry. Asterisks are expected, not a bug. See [[logging]].

Events actually emitted include `MessageReceived`, `UserActivity`, `ConversationStarted`, `LLMIntent`, `UserFeedback`, `BotResponseTime`, `AgentMessage`, `ChannelSessionCreated`, `DailyUserLimitReached`, and the notification family (`*NotificationSent` / `*NotificationFailed`, `RecipientAgentNotFound`, `WhatsAppTemplateLoadFailed`, `FCMTokensNotFound`).

**`azureDashboard.kql` query 2 uses `UserCreated`, which nothing in the code emits** — that panel is always empty. Don't read it as "no new users".

## First checks by symptom

| Symptom | Check in this order |
|---|---|
| **Bot doesn't answer a customer** | Thread `userState` — `AGENT`/`QUEUED` means an agent owns it and the bot stays quiet ([[live-agent-handover]]) · `isPaused` / `PAUSED` ([[email-channel]]) · `DailyUserLimitReached` ([[usage-enforcement]]) · exceptions for that `threadId` |
| **`503 Bot configuration not available`** | Config service reachability, then whether that `botId` exists. `POST /api/reset` rebuilds one bot's instance ([[multitenancy]]) |
| **Tenant sees stale config** | Did a `PLATFORM_CONFIG` callback arrive? Derived Redis caches and the LLM singleton only clear via `refreshPlatformConfig` / `/api/reset` ([[llm-and-prompts]]) |
| **Portal missing live updates** | Room scoping (`bot:<botId>`), sticky sessions for polling clients, and `SOCKET_DEBUG=true` for join/emit lines. Room sizes are per pod ([[chat-socket-events]]) |
| **Email not being answered** | The skip lines: self-sent echo, deferred to bot mailbox, duplicate claim, classified not relevant · a draft waiting for approval · a draft stuck in `SENDING` ([[email-channel]]) |
| **WhatsApp message never delivered** | Delivery status is processed **after** the 200 ack, so look for `WHATSAPP_STATUS_LOG_FAILED`; then Meta error codes (`132xxx` for templates) ([[whatsapp-templates]]) |
| **Call with no audio / stuck call** | The invariants list in [[whatsapp-calling]] — these fail silently by design of the underlying libraries |
| **Limits not being enforced** | Search log messages for `enforcement_*_fail_open`: the service fails open when it can't be reached ([[usage-enforcement]]) |
| **Process restarts / memory growth** | `/health` `applicationStartTime` and listener usage %; `Unhandled Promise Rejection` lines from the global guard |

## Reproducing safely
- Prefer devtest with a test tenant. `POST /api/reset` is safe (it rebuilds an instance) but drops in-memory per-call state, so don't run it while calls are live.
- Never run a broadcast, proactive send or email compose against a production tenant to test something.

## Checklist
- [ ] Filtered by `botId` and a time window
- [ ] Correlated on a business id, not `correlationId`
- [ ] Checked whether the feature fails open or logs only (enforcement, status webhooks, notifications)
- [ ] Confirmed which pod/instance you're looking at before trusting per-pod numbers
