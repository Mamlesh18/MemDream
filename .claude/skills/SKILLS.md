# inflow-cx-repo — Claude Code skills

Team-standard skills for this repository. Each folder has a `SKILL.md` that Claude Code loads when the task matches its description. You can also run one directly by typing `/<skill-name>`.

## Foundations — how any code here is written

| Skill | Use it when |
|---|---|
| [api-endpoint](api-endpoint/SKILL.md) | Adding or changing an Express route in `src/index.ts` |
| [service](service/SKILL.md) | New service class in `src/services`, wiring it into `bot.ts` / `BotInstanceBundle` |
| [data-access](data-access/SKILL.md) | MongoDB models, queries, upserts; Redis keys, TTLs, locks; tenant config |
| [error-handling](error-handling/SKILL.md) | try/catch, throw vs null, `ERROR_CODE` / `Modules`, custom errors |
| [logging](logging/SKILL.md) | `Logger`, telemetry events, APM, keeping PII out of logs |
| [multitenancy](multitenancy/SKILL.md) | `getOrCreateBotInstance`, botId/clientId resolution, config invalidation, per-pod caches |

## Message pipeline

| Skill | Use it when |
|---|---|
| [channel-adapter](channel-adapter/SKILL.md) | New or changed channel in `src/custom-adapters` |
| [bot-middleware](bot-middleware/SKILL.md) | Bot Framework middleware and its order |
| [dialog](dialog/SKILL.md) | Dialogs in `src/dialogs`, LG text, waterfall steps |
| [bot-state](bot-state/SKILL.md) | `UserProfile` / `ConversationData` accessors, `StateHelper`, eTags, state vs collection |
| [skill-packages](skill-packages/SKILL.md) | Registering, gating or bumping an `@inextlabs` vertical skill package |
| [attachments-media](attachments-media/SKILL.md) | Provider media, Blob upload, transcription, image extraction, size limits |

## Features

| Skill | Use it when |
|---|---|
| [whatsapp-calling](whatsapp-calling/SKILL.md) | WhatsApp voice calls, WebRTC, LiveKit, call history |
| [email-channel](email-channel/SKILL.md) | Inbound email guards, AI drafts and approval, pause/resume, Logic App sending |
| [live-agent-handover](live-agent-handover/SKILL.md) | Queueing to an agent, availability, AOS routing, `USER_STATE` transitions |
| [chat-socket-events](chat-socket-events/SKILL.md) | Socket.IO events and rooms shared with the agent portal |
| [llm-and-prompts](llm-and-prompts/SKILL.md) | Azure OpenAI clients, model config, classification, prompts and their caches |
| [usage-enforcement](usage-enforcement/SKILL.md) | Quotas, AI credits, limit denials, metering a new feature |
| [proactive-messaging](proactive-messaging/SKILL.md) | Sending outside a turn: `continueConversation`, timers, the 24-hour window |
| [whatsapp-templates](whatsapp-templates/SKILL.md) | Template rendering and placeholders, status webhooks, wamid, Meta error codes |
| [broadcast-and-campaign](broadcast-and-campaign/SKILL.md) | Broadcast replies and session seeding, Campaign Entry outcomes |
| [notifications](notifications/SKILL.md) | Agent mentions, multi-channel fan-out, FCM tokens |

## Process and operations

| Skill | Use it when |
|---|---|
| [git-workflow](git-workflow/SKILL.md) | Branch, commit, PR, dependency bump, merge readiness — manual only (`/git-workflow`) |
| [local-setup](local-setup/SKILL.md) | Setting the repo up on a new machine, or a local run failing to start |
| [build-and-deploy](build-and-deploy/SKILL.md) | Dockerfile, the four pipelines and their gates, image tags, promotion, rollback |
| [debug-production](debug-production/SKILL.md) | Investigating a live issue: KQL, APM, `/health`, first checks per symptom |
| [security-checklist](security-checklist/SKILL.md) | New endpoints, secrets, tenant isolation, uploads, PII — before merging |
| [testing](testing/SKILL.md) | Adding tests: where they go, what's worth testing first, the broken scripts |

## How they fit together

```
                    git-workflow  (every change)
                          │
  multitenancy ── api-endpoint ── service ── data-access
        │                             ▲
        │                             │
 channel-adapter ─ bot-middleware ─ dialog
        │                │             │
        │                │             └── llm-and-prompts ── usage-enforcement
        │                │
        ├── email-channel ── chat-socket-events
        ├── live-agent-handover ── chat-socket-events
        └── whatsapp-calling ── chat-socket-events

  error-handling + logging apply to all of the above
```

## Writing or changing a skill
- One skill = one task area. Keep `SKILL.md` short (under ~200 lines). Put long reference material in extra files in the same folder and link to them.
- The frontmatter `description` decides when Claude loads the skill. Write it as "Use when …" and name the concrete files and tasks.
- Skills must describe **how this repo actually works**. When a pattern changes in code, update the skill in the same PR.
- Skills with side effects (commits, PRs, deploys) set `disable-model-invocation: true`, so they only run when someone asks.
- Don't add a skill for something an existing one covers. Channel specifics belong in `channel-adapter`, `openapi.json` rules in `api-endpoint`, Redis key rules in `data-access`.
- Test a change by starting a fresh Claude Code session, asking a normal task question ("add an endpoint to archive a thread"), and checking that the right skill loads and its steps are followed.

## Not covered yet

Candidates nobody has written: proactive messaging · broadcast and campaigns · notifications (mentions, FCM) · WhatsApp templates · attachments and media · bot state · local setup · debug-production (KQL) · deploy pipelines · Docker build · security checklist · testing (needs a decision first — `pnpm test` has no tests).
