---
name: service
description: Use when creating a new service class in src/services, adding methods to an existing service, or wiring a service so routes, dialogs, and middleware can use it (initializeBot in bot.ts and BotInstanceBundle in index.ts).
---

# Services

Business logic lives in `src/services/`. Routes, dialogs, middleware and adapters stay thin and call services.

Related: [[data-access]], [[error-handling]], [[logging]], [[api-endpoint]].

## Naming
- File: `src/services/<kebab-name>.service.ts`, e.g. `broadcast-reply.service.ts`.
- Class: `<PascalName>Service`, e.g. `BroadcastReplyService`.
- Some older files are camelCase without `.service` (`emailDraftService.ts`, `outboundEmailService.ts`). Don't copy that in new files, and don't rename old ones in unrelated PRs.

## Two kinds of service

| Kind | Example | How it's created |
|---|---|---|
| **Per-tenant** (needs botId, clientId, tenant DB) | `ThreadService`, `ContactService`, `MessageService` | `new` inside `initializeBot()` in `src/bot.ts`, one instance per bot |
| **Process-wide** (no tenant state) | `LlmService`, `KeyVaultService` | `private constructor` + `static getInstance()` |

Most new services are per-tenant.

## Per-tenant template

```ts
import { BotTelemetryClient } from 'botbuilder';
import { Model } from 'mongoose';
import { ThingDocument } from '@inextlabs/schema-models';
import { DataConfigService } from './data-config.service';
import { ThreadService } from './thread.service';
import { Logger } from '../utils/logger';
import { ERROR_CODE, Modules } from '../enum/index';

export interface ThingResult { ... }            // public input/output types live at the top

export class ThingService {
    private logger: Logger;
    private thingModel: Model<ThingDocument>;

    constructor(
        private dataConfigService: DataConfigService,   // 1st: always
        telemetryClient: BotTelemetryClient,            // 2nd: always
        private threadService: ThreadService            // then dependencies
    ) {
        try {
            this.logger = new Logger(telemetryClient, dataConfigService.botId, dataConfigService.clientId);
            this.thingModel = dataConfigService.getThingModel();
        } catch (error: any) {
            this.logger.error(error, 'constructor', 'Error initializing ThingService',
                Modules.THING_SERVICE, ERROR_CODE.INITIALIZATION_FAILED);
            throw error;
        }
    }

    /**
     * What it does, and what null means (e.g. "null when no thing exists for this id").
     */
    async findThing(thingId: string): Promise<ThingDocument | null> {
        try {
            return await this.thingModel.findOne({ botId: this.dataConfigService.botId, thingId }).lean();
        } catch (error: any) {
            this.logger.error(error, 'findThing', 'Error finding thing',
                Modules.THING_SERVICE, ERROR_CODE.FIND_THING_FAILED, { thingId });
            throw error;
        }
    }
}
```

## Rules
- Constructor order is `dataConfigService`, `telemetryClient`, then dependencies. Only take the dependencies you use.
- Get Mongo models from `dataConfigService.getXModel()` in the constructor. Every query is scoped with `botId: this.dataConfigService.botId`. See [[data-access]].
- Services never import Express `Request`/`Response` or send HTTP responses. Return data or throw.
- Services never send user-facing text directly. Dialogs and adapters do that.
- Env vars: read `process.env.X` in one small helper function with a fallback, like `getReportTableName()` in `broadcast-reply.service.ts`. Add each var to `.env.example` under the right section.
- Cache expensive SDK clients at module level (`let cachedTableClient: TableClient | null = null`), not per call.
- Types used only by this service are exported from the service file. Types shared across files go in `src/types/types.ts`.
- JSDoc on public methods should explain **why** and what the edge cases are (null meaning, idempotency, race safety), not restate the code.
- If a service needs another service, inject it. Don't `new` it inside the class (a few older dialogs do; don't copy that).

## Wiring a per-tenant service

To use it from a route (`botInstance.thingService`), change four places:

1. **`src/bot.ts` → `initializeBot()`**: construct it after its dependencies exist.
   `const thingService = new ThingService(dataConfigService, telemetryClient, threadService);`
2. **`src/bot.ts` → `return { ... }`** at the end of `initializeBot`: add `thingService`.
3. **`src/index.ts` → `interface BotInstanceBundle`**: add `thingService: ThingService;` (`| undefined` if it only exists when a channel is configured).
4. **`src/index.ts` → `recreateBotFor()`**: map `thingService: initResult.thingService`.

A channel-conditional service (like `emailDraftService`) is created inside that channel's `if` block in `bot.ts`. Routes must check it exists and return `404 "… not configured for this bot"`.

Dialog-only services can be passed straight into the dialog's constructor from `initializeBot` and skip steps 3–4.

## Checklist
- [ ] `kebab-name.service.ts` / `PascalNameService`
- [ ] Constructor: `dataConfigService`, `telemetryClient`, deps; try/catch with `INITIALIZATION_FAILED`
- [ ] Every query scoped by `botId`
- [ ] Every method follows [[error-handling]] (log + throw, or documented `null`)
- [ ] `Modules` and `ERROR_CODE` entries added
- [ ] Wired in all four places if routes use it
