---
name: skill-packages
description: Use when adding, registering, gating, or upgrading an @inextlabs skill package (vertical/customer flows such as booking, online ordering, catalog, feedback) — SkillName entries, skillConfig gating in MainDialog.addSkills, or bumping a package version.
---

# @inextlabs skill packages

Customer and vertical behaviour lives in private npm packages, not in this repo. This repo registers them and routes to them.

Related: [[dialog]], [[git-workflow]], [[data-access]].

## The two kinds of package

| Kind | Examples | Role |
|---|---|---|
| **Shared platform** | `schema-models`, `shared-bot-state`, `lg-template`, `message-service`, `app-insights-helper` | Used everywhere; changes ripple across the repo |
| **Vertical / customer skills** | `booking-skill`, `whatsapp-online-order`, `advance-catalog-skills`, `feedback-skill`, plus per-customer packages | Own dialogs, registered conditionally |

All of them come from the private GitHub registry configured in `.npmrc`.

## Registration

`MainDialog.addSkills()` in `src/dialogs/main.dialog.ts`:

```ts
// config loaded once, in parallel
const [redisClient, botConfig, lgTemplate, skillConfig, response] = await Promise.all([...]);

const booking = skillConfig.skills?.find(s => s?.name?.toLowerCase() === SkillName.APPOINTMENT_BOOKING.toLowerCase());
if (booking?.enabled) {
  this.addDialog(new BookAppointmentDialog(SkillName.APPOINTMENT_BOOKING, booking, conversationState, userState));
}
```

Rules:
- `skillConfig.skills[]` entries have `name`, `enabled` and `configuration`. Names are UPPER-KEBAB (`AGENT-ASSIST`, `WHATSAPP-ONLINE-ORDER`).
- Compare names **case-insensitively**, exactly as the existing blocks do.
- Gate on `enabled`. A missing or disabled skill is simply not registered — this must **never throw**. One tenant's bad config cannot stop the bot from starting.
- The dialog id is usually the `SkillName` value; generic in-repo dialogs use the `DIALOGS` enum instead.
- Pass what the package asks for: its own `configuration` object, the states, `dataConfigService`/`botId`/`clientId`/`telemetryClient`, and the response queue client when the package writes outcomes.
- Load config once at the top of `addSkills` and reuse it. Don't fetch config per skill.

## Adding a package

1. Add the dependency, then `pnpm install` and commit `pnpm-lock.yaml`.
2. Import only the dialogs you register — `main.dialog.ts` already has a very large import list, and every package is loaded for every tenant.
3. Add a `SkillName` entry whose value matches the config name **exactly**.
4. Register it in `addSkills`, gated on `enabled`.
5. Make sure intent or skill matching in the main waterfall can reach it.
6. Note the new LG template keys and config the tenant must have, in the PR.

## Upgrading
- Change the version in `package.json` in that package's existing style (exact vs `^`), `pnpm install`, commit the lockfile in the same commit: `chore(deps): bump @inextlabs/<pkg> to <version>`.
- A vertical package carries its own dialogs, prompts and LG keys, so a bump can change customer-visible behaviour. Exercise one tenant flow on devtest before merging. See [[git-workflow]].
- A `schema-models` bump can change documents and model factories — check every `DataConfigService` getter you touch. See [[data-access]].

## Don't fork vertical logic

If a customer flow is wrong, fix it in the package and bump. Copying dialog logic into `src/dialogs/` makes this repo a customer-specific fork, which is exactly what the package split exists to prevent. This repo owns only generic plumbing: adapters, middleware, state, routing, live agent, calling, email.

## Checklist
- [ ] `SkillName` value matches the config name exactly
- [ ] Gated on `enabled`; disabled or missing config never throws
- [ ] Config loaded once in `addSkills`; imports kept narrow
- [ ] Reachable from intent/skill matching
- [ ] Lockfile committed with the version change
- [ ] No vertical logic copied into this repo
