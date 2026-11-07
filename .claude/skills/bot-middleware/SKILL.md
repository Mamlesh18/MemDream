---
name: bot-middleware
description: Use when creating or changing Bot Framework middleware in src/middleware (user, logger, typing, feedback, suggested actions, daily limit, AI pause, playground), intercepting outbound activities with onSendActivities, sharing per-turn data through turnState, or changing middleware order in bot.ts.
---

# Bot Framework middleware

Middleware runs on every turn of every adapter it's registered on: before the dialog (`onTurn`), and around outgoing messages (`onSendActivities`).

Related: [[channel-adapter]], [[dialog]], [[error-handling]].

## Naming
- File: `src/middleware/<camelName>.middleware.ts`, e.g. `aiPause.middleware.ts`.
- Class: `<Name>Middleware implements Middleware` (from `botbuilder-core`).

## Template

```ts
import { ActivityTypes, BotTelemetryClient, Middleware, Severity, TurnContext } from 'botbuilder-core';
import { ERROR_CODE, Modules } from '../enum/index';
import { Logger } from '../utils/logger';

/**
 * What it gates or adds, and WHERE it must sit in the chain and why.
 */
export class ThingMiddleware implements Middleware {
  private logger: Logger;

  constructor(private dataConfigService: DataConfigService, telemetryClient: BotTelemetryClient) {
    try {
      this.logger = new Logger(telemetryClient, dataConfigService.botId, dataConfigService.clientId);
    } catch (error: any) {
      new Logger(telemetryClient).error(error, 'constructor', 'Error initializing ThingMiddleware',
        Modules.MIDDLEWARE, ERROR_CODE.INITIALIZATION_FAILED);
      throw error;
    }
  }

  public async onTurn(context: TurnContext, next: () => Promise<void>): Promise<void> {
    if (context.activity?.type !== ActivityTypes.Message) {
      return next();                                  // exit early for activities you don't handle
    }

    const userData = context.turnState.get('userData'); // set by UserMiddleware — don't re-query DB

    // ... work ...

    await next();                                     // ALWAYS, unless deliberately stopping the turn
  }
}
```

## Rules
- **Always `await next()`** unless the middleware exists to stop the turn. When you stop it, log why and write a comment (see `AiPauseMiddleware`).
- **Exit early** for activity types you don't handle, e.g. `return next()` for non-messages.
- **Reuse turn data.** `UserMiddleware` puts `{ user, contact, thread, conversation, ... }` in `context.turnState.get('userData')`. Read that instead of querying Mongo again.
- **Outbound interception** goes through `context.onSendActivities(async (ctx, activities, nextSend) => { ...; return await nextSend(); })`. Handlers run in **reverse** registration order.
- **Failures:** non-critical work (typing, analytics, suggested actions) catches and logs, and the turn continues. Critical work (user or contact creation, enforcement) lets the error reach the adapter's `onTurnError`.
- **Blocking:** by default, don't make the user wait on side work (fire it, with `.catch` logging). Await only when a later step depends on it. Example: for EMAIL, `LoggerMiddleware` awaits the inbound insert so a paused thread's message is really persisted before `AiPauseMiddleware` stops the turn.
- One instance per bot, created in `initializeBot()` and shared across that bot's adapters.

## Order (in `src/bot.ts`)

The order is part of correctness. When you change it, update the order comment in `bot.ts`.

| Adapter | `onTurn` order |
|---|---|
| CloudAdapter (web / Direct Line) | Playground → Feedback → Typing → User → Logger → SuggestedActions → DailyUserLimit |
| WhatsApp | Feedback → User → Typing (WhatsApp client) → Logger → DailyUserLimit |
| Instagram, RCS | Feedback → User → Logger → DailyUserLimit |
| Email | Feedback → User → Logger → **AiPause** → DailyUserLimit |

Invariants:
- `User` runs before `Logger`, because Logger needs `userData`.
- `Logger` runs before anything that can stop the turn, so the inbound message is always saved.
- `Playground` is first on CloudAdapter, so its `onSendActivities` runs last and restores `directline`.
- Register a new middleware on **every** adapter where it applies, not just CloudAdapter.

## Checklist
- [ ] Constructor try/catch with `INITIALIZATION_FAILED`
- [ ] Early exit for unhandled activity types; `await next()` on every continuing path
- [ ] Uses `turnState` `userData` instead of extra DB reads
- [ ] Registered in the correct position on every relevant adapter; order comment updated
