---
name: git-workflow
description: Use when creating a branch, writing a commit message, preparing or describing a pull request, bumping an @inextlabs package, or checking a change is ready to merge in this repo.
disable-model-invocation: true
---

# Branch, commit and PR workflow

## Environments

| Branch | Pipeline | Deploys to |
|---|---|---|
| `devtest` | `azure-pipeline-devtest.yml` | DevTest |
| `staging` | `azure-pipeline-staging.yaml` | Staging |
| `main` | `azure-pipeline-prod.yaml` | Production |

Flow: **feature branch → PR into `devtest` → `staging` → `main`.** Never open a feature PR straight into `staging` or `main`. Pipelines deploy on push to these branches, so merging is deploying.

## Branches
- Start from an up-to-date `devtest`: `git fetch origin && git checkout -b <name> origin/devtest`.
- Name: `<type>/<short-kebab-desc>`, with the Azure Boards id when there is one: `feat/AB#20399-broadcast-reply`, `fix/call-accept-duration`, `chore/bump-kwi-skill`.
- Types: `feat`, `fix`, `refactor`, `chore`, `docs`.

## Commits
- Conventional Commits: `<type>(<scope>): <imperative summary>`. Lowercase, no trailing period, about 72 characters.
- Scope is the feature area: `email`, `call`, `livekit`, `broadcast`, `adapter`, `dialog`, `api`, `deps`, `docker`.
- Link work items with `AB#<id>` in the subject or body.
- Good: `feat(email): add pause/resume endpoints for AI replies AB#20706`
- Not acceptable: `Update index.ts`, `changes`, `fix: resolved conflicts` for a commit that actually changes behaviour.
- One logical change per commit. Keep formatting-only changes out of feature commits (`index.ts` is 6000+ lines, and noisy diffs hide bugs).

## Before opening a PR
- [ ] `git fetch && git rebase origin/devtest` (or merge), with conflicts resolved.
- [ ] `pnpm build` passes. This runs `tsc` and is the **only reliable automated check today**.
  - `pnpm lint` points to a missing `tslint.json`, and `pnpm test` has no test files. Don't claim lint or tests passed.
- [ ] Nothing secret is staged: no `.env*`, keys, tokens or connection strings. Don't edit `src/resources/*.json` credentials. Check `git diff --cached`.
- [ ] `build/` and `temp/` aren't committed (they're gitignored; don't force-add them).
- [ ] Routes added or changed → `openapi.json` updated. See `api-endpoint`.
- [ ] New env vars → `.env.example` updated with a comment, in the right section.
- [ ] New `ERROR_CODE` values are unique. See `error-handling`.
- [ ] Debug `console.log` lines removed.
- [ ] Call-flow changes checked against the `whatsapp-calling` invariants.

## Dependency bumps (`@inextlabs/*`)
- Change the version in `package.json` following that package's existing style (exact vs `^`), run `pnpm install`, and commit `pnpm-lock.yaml` in the same commit.
- Commit: `chore(deps): bump @inextlabs/<pkg> to <version>`.
- The private registry needs the GitHub token set up in `.npmrc`. Don't commit a token.

## PR description

```markdown
## What
<one or two sentences>

## Why
<problem / AB#id>

## How
- <key change 1>
- <key change 2>

## Config / migration
- Env vars: <none | NEW_VAR in .env.example>
- LG template keys / channel config / schema-models version: <none | …>

## Testing
- `pnpm build` ✅
- Manual: <env (local/devtest), steps, result>

## Risk
<what could break, how to roll back>
```

## Merging
- Resolve review comments before merging. Delete the branch after merge.
- Promote `devtest → staging → main` through PRs between those branches, not by cherry-picking feature commits.
