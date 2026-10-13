---
name: logging
description: Use when adding logs, telemetry, events, or APM tracing anywhere in this repo — choosing between Logger.log / Logger.error / trackEvent / console, what details to attach, severity levels, and keeping PII (phone, email, names, message bodies) out of logs.
---

# Logging and telemetry

Everything goes to Application Insights through `Logger` (`src/utils/logger.ts`). Elastic APM is used for tracing on hot paths.

Related: [[error-handling]].

## Create one Logger per class

```ts
import { Logger } from '../utils/logger';
import { Severity } from 'botbuilder-core';
import { ERROR_CODE, Modules } from '../enum/index';

this.logger = new Logger(telemetryClient, dataConfigService.botId, dataConfigService.clientId);
```

- Build it once in the constructor, not per call. The exception is route handlers in `index.ts`, which create `new Logger(telemetryClient, botId || "unknown", botInstances.get(botId)?.dataConfig.clientId)` inside `catch`.
- `botId` and `clientId` are attached automatically. Don't repeat them in `details`.

## API

| Call | When |
|---|---|
| `logger.log(message, Severity.Information, details)` | Business milestones: thread paused, call routed to AI, draft held. |
| `logger.log(message, Severity.Warning, details)` | Unexpected but handled: missing optional config, fallback used, retry. |
| `logger.error(err, methodName, description, Modules.X, ERROR_CODE.X, details)` | Every caught failure. See [[error-handling]]. |
| `logger.trackEvent(name, properties, metrics)` | Countable business events for dashboards: call accepted, draft approved. Not for errors. |

Don't call `telemetryClient.trackException` or `trackTrace` directly in new code. Going through `Logger` keeps the property structure (correlationId, service, environment, errorCode) the same everywhere.

## What to put in `details`
- Always `module: Modules.X` on `log` calls. `error` takes the module as a parameter.
- Correlating ids for whatever you're working on: `threadId`, `callId`, `messageId`, `userId`, `agentId`, `jobId`.
- Small, structured values. Never whole payloads, activities, SDPs or request bodies.

```ts
this.logger.log('AI responses paused for this thread', Severity.Information, {
    module: Modules.EMAIL_ADAPTER,
    threadId: thread?.threadId,
});
```

## PII
- `Logger` masks values whose **key** contains `name`, `phone`, `email`, `address`, `userName`, `customerPhone` and similar. So pass PII only as a `details` property with such a key, e.g. `{ customerPhone }`.
- The **message string is not masked**. Never interpolate phone numbers, emails, names or message text into it.
- `console.*` is **not masked**. Never console-log payloads, activities, email bodies or phone numbers.

## console

`console.*` is still used heavily for local debugging, and in production `index.ts` wraps it into single-line JSON. Rules for new code:
- Prefix with the component in brackets: `console.error("[BroadcastReply] ...")`, `console.error("[api/email/compose] Error:", err)`.
- `console.error` in a route `catch` is fine, **alongside** `Logger.error`, never instead of it.
- No `console.log(JSON.stringify(req.body))` or leftover debug logs in a PR.
- `Logger.isDev()` currently always returns `true`, so every `Logger` call also prints to the console. Don't add an extra `console.log` for the same line.

## Elastic APM (hot paths)

Services on the message path (thread, user, conversation, channelSession) wrap DB calls in transactions:

```ts
const transaction = apm.startTransaction('ThreadService.findThread', 'service');
try {
    ...
    if (transaction) { transaction.result = 'success'; transaction.end(); }
} catch (error) {
    ...
    if (transaction) { transaction.result = 'error'; transaction.end(); }
    throw error;
}
```

Name transactions `ClassName.methodName`, with type `service`, `adapter` or `middleware`. Add them only on per-message or per-call paths, not one-off admin routes.

## Finding logs
- App Insights `traces` and `exceptions`, filtered by `customDimensions.botId`, `errorCode`, `name` (the method or logger title).
- Starter queries are in `azureDashboard.kql` at the repo root.
- `WaterfallStart`, `WaterfallStep` and `WaterfallComplete` events are dropped by `FilteredTelemetryClient`.

## Checklist
- [ ] Uses `Logger`, created once per class
- [ ] Every `log` has `module`, every `error` has `Modules` + `ERROR_CODE`
- [ ] No PII in message strings or console output
- [ ] No debug `console.log` left behind
- [ ] Correlating id (`threadId`, `callId`, …) attached
