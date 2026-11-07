---
name: security-checklist
description: Use when reviewing or writing anything with a security dimension in this repo — a new endpoint, webhook verification, secret handling, tenant isolation, PII in logs, blob storage exposure, or before merging a change that touches auth, uploads, or customer data.
---

# Security rules for this repo

Related: [[api-endpoint]], [[logging]], [[multitenancy]], [[build-and-deploy]].

## Know the accepted posture

Documented in `openapi.json`, and deliberate rather than an oversight:
- **Most Express routes have no inbound authentication.** CORS is `origin: "*"` and no auth middleware is mounted globally.
- `bot-id`, `agent-id`, `user-id`, `thread-id` and `:botId` are **routing identifiers, not credentials**. They are read straight from headers with no verification.

The consequences you must design around:
- Never treat those headers as proof of identity or authorization.
- Assume any route can be called by anyone who knows the URL. So bound every size, validate every input, and make destructive operations idempotent or guarded.
- Don't "fix" the posture in a feature PR. Raise it separately.

## The routes that do authenticate

| Guard | Where | How |
|---|---|---|
| `x-internal-api-key` | Email agent routes (approve, reject, compose, pause, resume) | Constant-time compare against `INTERNAL_API_KEY`; `503` if unset, `401` if wrong. Service-level only — it does **not** prove the caller is `agent-id` or that the agent belongs to the tenant |
| `x-email-shared-secret` | Inbound email webhook | Per-bot secret from channel config, constant-time compared |
| `clientToken` | RCS webhook | Compared against the channel's `webhookSecret` |

Rules:
- Any new route that can **send** on a tenant's behalf (message, email, broadcast, call) or change tenant state gets at least the internal key guard.
- Compare secrets with `crypto.timingSafeEqual` on equal-length buffers, never `===`.
- Mount the guard **before** multer, or an unauthenticated caller can make the process buffer files.

## Tenant isolation — the highest-impact risk
- Every Mongo query is scoped by `botId`. See [[data-access]].
- Socket emits are room-scoped; **never `io.emit`** tenant data. See [[chat-socket-events]].
- Notification recipients are resolved within the bot/client. See [[notifications]].
- Nothing derived from one tenant's config may be cached where another tenant's request reads it. See [[multitenancy]].

## Secrets
- Model and provider keys come from **Key Vault** (`KeyVaultService`), infra credentials from env. Never inline a key, endpoint secret or connection string.
- `.env*` files are gitignored. Check `git diff --cached` before committing, and never paste secrets into logs, tests, comments or PR descriptions.
- The private registry token is read from `${AuthToken}` in `.npmrc`. Don't hardcode it.
- **Known risks not to worsen:** the Dockerfile keeps `ENV AuthToken` in the final image, and `.dockerignore` misses `.env.local` / `.env.devtest` / `.env.staging` / `.env.customerdev`, so a local `docker build` can bake them in. See [[build-and-deploy]].

## Uploads and untrusted input
- Bound count and size **before** buffering or decoding (`/api/agent` derives the decoded size from base64 length first). Reject with `400`/`413`.
- Validate types against `SUPPORTED_ATTACHMENT_*`. See [[attachments-media]].
- Email attachments stay in memory and are never fetched from a caller-supplied URL — that absence of a URL-fetch path is what keeps SSRF out. Don't add one.
- The blob container `<botId>-others` is **public read**. Only customer-shareable media goes there — never exports, reports or internal documents.

## PII and logging
- Never log message bodies, email subjects or bodies, phone numbers, addresses, SDPs, prompts, completions, or attachment bytes.
- `Logger` masks values by key name; `console.*` masks nothing.
- Log shape and ids instead, the way `/api/agent` and the email webhook do. See [[logging]].

## Errors
Return `err?.message` with a safe fallback. Never a stack trace, a raw provider payload, or a database error. See [[error-handling]].

## Pipeline gates (production only)
TruffleHog secret scan, SonarQube quality gate, Snyk SCA at `high`, Trivy image scan. A secret committed anywhere in the tree fails the prod pipeline — but devtest and staging have no scans, so don't rely on CI to catch a leak.

## Before merging
- [ ] No secret, token or `.env*` file staged
- [ ] New mutating/sending route is guarded; guard before multer
- [ ] Every query and emit scoped to the tenant
- [ ] Sizes and types bounded before work
- [ ] No PII in logs or error responses
- [ ] No new public-blob or caller-supplied-URL fetch path
- [ ] For a broader pass on a branch, run the built-in `/security-review`
