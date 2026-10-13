---
name: dialog
description: Use when creating or changing a Bot Framework dialog in src/dialogs (main, live agent, agent intelligent, subscribe/unsubscribe), registering an @inextlabs skill-package dialog in MainDialog, reading or writing user/conversation state, or sending user-facing text from a dialog via LG templates.
---

# Dialogs

Related: [[bot-middleware]], [[service]], [[error-handling]].

## What belongs in this repo
- **Generic, platform-wide dialogs** live in `src/dialogs/`: `MainDialog`, `LiveAgentDialog`, `AgentIntelligentDialog`, `SubscribeDialog`, `UnsubscribeDialog`.
- **Customer or vertical flows** (booking, ordering, catalog, a hospital's appointment flow) live in private `@inextlabs/*` skill packages. This repo only **registers** them in `MainDialog`. Don't build customer-specific flows here.

## Naming
- File: `src/dialogs/<camelName>.dialog.ts`. Class: `<Name>Dialog extends ComponentDialog`.
- The dialog id goes in the `DIALOGS` enum (`src/enum/index.ts`). The inner waterfall id is a file-level `const`.

## Template

```ts
import { ComponentDialog, WaterfallDialog, WaterfallStepContext, DialogTurnResult } from 'botbuilder-dialogs';
import { StatePropertyAccessor } from 'botbuilder';
import { CustomConversationState, CustomUserState, ConversationData, UserProfile } from '@inextlabs/shared-bot-state';
import { UserDocument, ContactDocument, ConversationDocument } from '@inextlabs/schema-models';
import { getDynamicContent } from '@inextlabs/lg-template';
import { DataConfigService } from '../services/data-config.service';
import { GENERAL } from '../enum';

const WATERFALL_DIALOG = 'thingWaterfallDialog';

export class ThingDialog extends ComponentDialog {
    private userProfileAccessor: StatePropertyAccessor<UserProfile>;
    private conversationDataAccessor: StatePropertyAccessor<ConversationData>;

    constructor(
        dialogId: string,
        conversationState: CustomConversationState,
        userState: CustomUserState,
        private dataConfigService: DataConfigService,
        private thingService: ThingService
    ) {
        super(dialogId);
        this.userProfileAccessor = userState.createProperty<UserProfile>(GENERAL.USER_PROFILE_PROPERTY);
        this.conversationDataAccessor = conversationState.createProperty<ConversationData>(GENERAL.CONVERSATION_DATA_PROPERTY);
        this.addDialog(new WaterfallDialog(WATERFALL_DIALOG, [
            this.doThingStep.bind(this),
        ]));
        this.initialDialogId = WATERFALL_DIALOG;
    }

    private async doThingStep(stepContext: WaterfallStepContext): Promise<DialogTurnResult> {
        const userData: { user: UserDocument; contact: ContactDocument; conversation: ConversationDocument } =
            stepContext.context.turnState.get('userData');
        if (!userData?.contact) {
            await stepContext.context.sendActivity('Unable to find your contact information.');
            return await stepContext.endDialog();
        }

        await this.thingService.doThing(userData.user.userId);

        const userProfile = await this.userProfileAccessor.get(stepContext.context, {} as UserProfile);
        const lgTemplate = await this.dataConfigService.getLgTemplate();
        const text = getDynamicContent(lgTemplate, userProfile.preferredLanguage, 'thing', 'thingSuccessMessage');
        await stepContext.context.sendActivity(text || 'Done.');

        return await stepContext.endDialog();
    }
}
```

## Rules

**User-facing text**
- Never hardcode messages. Use the tenant's LG template, so text is translated and configurable:
  - Plain text: `getDynamicContent(lgTemplate, userProfile.preferredLanguage, '<section>', '<key>')`
  - Channel-aware card: `getHandledDynamicCard(context.activity.channelId.toUpperCase(), lgTemplate, language, '<section>', '<key>')`
- A hardcoded string is allowed only as the `||` fallback. New LG keys must be added to the tenant LG template config, so mention them in the PR.

**Data and state**
- Read `userData` from `turnState` (set by `UserMiddleware`) instead of querying Mongo.
- Create accessors with the `GENERAL.*_PROPERTY` constants, never string literals.
- `DialogBot` saves user and conversation state at the end of each turn. **Don't call `saveChanges` inside steps.** The only exception is proactive or out-of-turn contexts (`continueConversation`), where nothing saves for you.
- Business logic goes through services. If an API route does the same thing, both call the **same** service method (`SubscribeDialog` and `POST /api/subscribe` both use `contactService.updateSubscriptionStatus`).
- Take services as constructor parameters, passed from `MainDialog` / `initializeBot`. Don't `new` services inside a dialog.

**Flow**
- Every step returns a `DialogTurnResult`: `stepContext.next()`, `prompt(...)`, `endDialog()`, `beginDialog(...)` or `replaceDialog(...)`.
- Don't catch errors just to hide them. The adapter's `onTurnError` clears state and sends the localized apology.
- **Email channel:** dialogs never send email directly. `AgentIntelligentDialog` marks replies as drafts (`value.draft`) and `LoggerMiddleware.onSendActivities` holds them for agent approval.

## Registering
- In the `MainDialog` constructor: `this.addDialog(new ThingDialog(DIALOGS.THING_DIALOG, conversationState, userState, dataConfigService, thingService));`
- Skill-package dialogs are registered only when that skill is enabled in `skillConfig`. Follow the existing blocks in `main.dialog.ts`.
- Routing to the dialog happens in the main waterfall (intent or skill matching). Add your case there, not in middleware.
- Bumping a skill package: exact version change in `package.json`, `pnpm install`, commit the updated `pnpm-lock.yaml`.

## Checklist
- [ ] Generic behaviour only; customer flows live in a skill package
- [ ] Id added to `DIALOGS`; registered in `MainDialog`
- [ ] Text from LG template with a fallback; new LG keys listed in the PR
- [ ] Uses `turnState` `userData` and injected services; no `saveChanges` in steps
- [ ] Every step returns a `DialogTurnResult`
