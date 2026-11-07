---
name: bot-state
description: Use when reading or writing bot state — UserProfile and ConversationData accessors, CustomUserState / CustomConversationState, the StateHelper storage backed by the Mongo state collection, eTag conflicts, saving state in proactive contexts, or deciding whether data belongs in state or in a collection.
---

# Bot state

Related: [[dialog]], [[data-access]], [[proactive-messaging]].

## How it's built

```
StateHelper (implements botbuilder Storage)  →  tenant DB `state` collection, _id = state key
   ↑
CustomUserState / CustomConversationState (@inextlabs/shared-bot-state), created in initializeBot
   ↑
accessors: userState.createProperty<UserProfile>(GENERAL.USER_PROFILE_PROPERTY)
```

Property names always come from the `GENERAL` enum: `USER_PROFILE_PROPERTY`, `CONVERSATION_DATA_PROPERTY`, `SKILL_CONVERSATION_DATA_PROPERTY`. A few old routes pass string literals — don't copy that.

## Reading and writing

```ts
const userProfile = await this.userProfileAccessor.get(context, {} as UserProfile);
userProfile.preferredLanguage = 'English';
await this.userProfileAccessor.set(context, userProfile);
```

- **In a normal turn, don't call `saveChanges`.** `DialogBot` saves both states at the end of the turn.
- **In a proactive context** (`continueConversation`) nothing saves for you — call `userState.saveChanges(context, false)` / `conversationState.saveChanges(context, false)` explicitly, as `onTurnError` does. See [[proactive-messaging]].
- Clearing the dialog stack is `conversationState.clear(context)` followed by `saveChanges`.

## What StateHelper does that will surprise you

**User keys and thread keys are written differently.** Keys containing `/users/` are written as a **flattened `$set`** (dot paths like `document.UserProfile.name`), so concurrent writes don't wipe sibling fields. Keys containing `/threads/` replace the whole `document`. A new key shape gets neither until you add it to `write()`.

**Writing UserProfile also updates the Contact document.** `write()` maps profile fields onto Contact (`displayName`, `mobile`, `emailId`, `country`, `city`, `state`, `latitude`, `longitude`, `browserName`, `preferredLanguage`, `preferredLanguageCode`), keyed by the channel-specific user id (`whatsappUserId`, `instagramUserId`, `facebookUserId`, `playgroundUserId`, else `webChatUserId`), and only writes when a field actually changed. So:
- A new profile field that also belongs on the contact needs adding to that mapping.
- Don't put large or fast-changing data in `UserProfile` — every write walks and compares it.

**eTag concurrency.** `__v` is the eTag. A write with an eTag updates only when `__v` matches, otherwise it throws `eTag conflict for key …`. Absent or `'*'` upserts. Don't swallow a conflict — let the turn fail and be retried, or you'll silently lose the other writer's changes.

## State or collection?

| Put it in state | Put it in a collection |
|---|---|
| Conversational context: preferred language, dialog stack, in-flight form answers, per-skill scratch data | Anything queried, reported, or shown in the portal: messages, threads, contacts, conversations, call history |

State is keyed per user or per conversation and can only be read through an accessor in a turn. **It is not a database** — you cannot query it, aggregate it, or show it in the portal. If you need to find records by a field, it belongs in a collection. See [[data-access]].

## Checklist
- [ ] Accessor names from the `GENERAL` enum
- [ ] No `saveChanges` in a normal turn; explicit save in proactive contexts
- [ ] New key shapes handled in `StateHelper.write()`
- [ ] New `UserProfile` fields added to the Contact mapping if they belong there
- [ ] eTag conflicts not swallowed
- [ ] Queryable or portal-visible data put in a collection, not state
