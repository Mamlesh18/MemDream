---
name: error-handling
description: Use when writing try/catch blocks, deciding whether to throw or return null, adding a new ERROR_CODE or Modules value, defining a custom error class, or handling errors inside services, adapters, middleware, dialogs, or routes in this repo.
---

# Error handling

Errors are handled differently at each layer. Pick the rule for the layer you're in.

Related: [[logging]], [[api-endpoint]], [[service]].

## By layer

| Layer | On error |
|---|---|
| **Route** (`src/index.ts`) | Catch everything → `console.error` + `Logger.error` → respond `{ error }` with the right status. See [[api-endpoint]]. |
| **Service / client method** | Catch → `this.logger.error(...)` → **rethrow** if the caller must react, or **return `null` / `[]`** when "nothing there" is a normal outcome. |
| **Constructor** (service, middleware, adapter) | Wrap the body in try/catch → log `ERROR_CODE.INITIALIZATION_FAILED` → rethrow. |
| **Adapter client** (HTTP to Meta, Google, Logic App) | Catch → log with `Modules.<CHANNEL>_CLIENT` → rethrow. The adapter decides what happens next. |
| **Middleware** | Non-critical work (typing, analytics) → catch and log, keep the turn alive. Critical work (user or contact creation) → let it bubble to `onTurnError`. |
| **Dialog step** | Don't catch just to hide an error. Let it reach the adapter's `onTurnError` in `bot.ts`, which clears state and sends the localized apology. |
| **Fire-and-forget promise** | Always attach `.catch(err => logger.error(...))`, e.g. `void p.catch(...)`. Never leave a floating promise. |

## Service method template

```ts
async findThing(id: string): Promise<ThingDocument | null> {
    try {
        return await this.thingModel.findOne({ botId: this.dataConfigService.botId, id }).lean();
    } catch (error: any) {
        this.logger.error(
            error,
            'findThing',                    // method name
            'Error finding thing',          // human description
            Modules.THING_SERVICE,          // module enum
            ERROR_CODE.FIND_THING_FAILED,   // error code enum
            { thingId: id }                 // correlating ids (PII keys are masked)
        );
        throw error;
    }
}
```

**Throw or return null?**
- Return `null` for an expected absence: not found, a reply that isn't a broadcast, a missing optional config. Say so in the JSDoc.
- Treat an Azure 404 as absence, not failure: `if (error?.statusCode === 404 || error?.code === 'ResourceNotFound') return null;` with no error log.
- Throw when the caller's result would be wrong without the value: DB down, provider rejected the send, invariant broken.
- Never `catch {}` with an empty body. If ignoring really is correct, write a comment explaining why.

## Domain errors

For conditions the caller must branch on, export an `Error` subclass from the owning service and check it with `instanceof`. Don't match message strings in new code.

```ts
export class ConversationLimitExceededError extends Error { ... }   // enforcement.service.ts
if (error instanceof ConversationLimitExceededError) { ... }        // bot.ts onTurnError
```

## Adapter onTurnError

- Standard channels use the shared `onTurnErrorHandler` in `bot.ts`. It clears conversation state and sends the first-time or second-time handled-exception message from the LG template.
- The **Email** adapter deliberately rethrows, so the webhook fails and the Logic App retries. Deterministic denials (`ConversationLimitExceededError`) are swallowed so they aren't retried forever. Keep that split for any retrying channel.

## Provider errors
- WhatsApp Graph API errors: use `generateFailureReason(error)` from `src/utils/whatsapp-error.util.ts` for a readable reason, and add new Meta codes to `WHATSAPP_ERROR_MESSAGES` there.
- Never pass raw provider payloads back to API clients.

## Adding ERROR_CODE and Modules values

Both live in `src/enum/index.ts`.

1. **Grep first.** The value may already exist, and the enum already has duplicate values (`INL2101`, `INL2103`, `INL2301`). Don't add more duplicates.
2. Add the code inside its feature's block, e.g. `// Email channel (INL2700–)`. Use the next free number in that block.
3. A new feature gets a new hundred-block after the highest block in use, with a header comment `// <Feature> (INLxx00–)`.
4. Name codes `<ACTION>_<OBJECT>_FAILED`, e.g. `SEND_MESSAGE_FAILED`, `FIND_THREAD_FAILED`.
5. A new service or adapter gets a `Modules` entry: `THING_SERVICE`, `<CHANNEL>_ADAPTER`, `<CHANNEL>_CLIENT`.

## APM

Services that already use Elastic APM transactions (thread, user, conversation, channelSession) also report handled errors:

```ts
apm.captureError(error, { handled: true, custom: { module: Modules.X, errorCode: ERROR_CODE.X, title: '...', method: 'methodName' } });
if (transaction) { transaction.result = 'error'; transaction.end(); }
```

End the transaction on **both** the success and error paths.

## Checklist
- [ ] No empty catch; every catch logs or has a comment saying why not
- [ ] Throw vs `null` chosen on purpose and documented
- [ ] `Modules` and `ERROR_CODE` values exist, are unique, and sit in the right block
- [ ] No stack traces or raw provider errors returned to clients
- [ ] Every fire-and-forget promise has a `.catch`
