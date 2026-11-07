---
name: local-setup
description: Use when setting up this repo on a new machine or debugging a local run — pnpm and the private registry token, native dependencies (wrtc, sharp, ffmpeg), env files per environment, running dev/start scripts, reaching webhooks from localhost, and common startup failures.
---

# Local setup and running

Related: [[build-and-deploy]], [[multitenancy]], [[debug-production]].

## Prerequisites
- **Node 20** (the image is `node:20-bookworm-slim`).
- **pnpm ≥ 9** via corepack: `corepack enable && corepack prepare pnpm@latest --activate`. `package.json` pins `pnpm@10.15.0`.
- **MongoDB** and **Redis** reachable from your machine.
- A **GitHub Packages token** with `read:packages` for the private `@inextlabs/*` registry.
- **Native build toolchain** for `wrtc`, `sharp` and `ffmpeg-static`. The `Dockerfile` is the authoritative list: `python3 make g++ pkg-config cmake libasound2-dev libx11-dev libxkbcommon-dev libudev-dev libnss3-dev libssl-dev zlib1g-dev libglib2.0-dev`.

## Install

`.npmrc` reads the token from an environment variable, so export it **before** installing:

```bash
export AuthToken=<github-packages-token>   # PowerShell: $env:AuthToken = "<token>"
pnpm install
```

Never hardcode the token in `.npmrc` and never commit one.

## Configure

Copy `.env.example` to `.env` and fill it in. Per-environment files are supported and all gitignored: `.env.local`, `.env.devtest`, `.env.staging`, `.env.customerdev`.

Minimum to boot: `MONGO_DB_CONNECTION_STRING` · `AZURE_CACHE_FOR_REDIS_HOST_NAME` + `AZURE_CACHE_FOR_REDIS_ACCESS_KEY` · `AZURE_BOT_*` credentials · `INSTRUMENTATIONKEY` · `CONFIG_SERVICE_BASE_URL` · the client/bot identity vars · `AZURE_STORAGE_ACCOUNT_NAME` + `_KEY`.

Optional groups: `ENFORCEMENT_*`, `LIVEKIT_*`, `FIREBASE_*`, `INTERNAL_API_KEY`, `TEMP_DIR`, `SOCKET_DEBUG`.

**Most behaviour is not in env.** Bot, channel, skill and LG configuration comes from the config service and MongoDB per tenant, so you need a real `botId`/`clientId` that exists there and a reachable config service. See [[multitenancy]].

## Run

```bash
pnpm run build        # tsc --build -> ./build
pnpm run dev          # watch mode, uses .env
pnpm run dev:local    # watch mode with .env.local   (also dev:devtest, dev:staging, dev:customerdev)
pnpm run start        # node ./build/index.js        (also start:local, start:devtest, …)
```

Default port is **3978**; override with `PORT`. On `EADDRINUSE` the bootstrap probes alternative ports and logs the one it picked, so check the startup output rather than assuming 3978.

Verify with `GET /health` — it lists which bots this process has built.

**`pnpm test` and `pnpm lint` do not work today.** `lint` points at a missing `tslint.json`, and there are no test files. See [[testing]].

## Reaching webhooks from localhost

Meta, Google RCS and the email Logic Apps cannot call `localhost`. Either:
- expose your port through a tunnel and point that bot's channel config webhook at the tunnel URL (remember it's tenant config, so you're changing it for everyone using that bot), or
- work against a bot deployed on devtest and test locally only with the Bot Framework Emulator / web chat.

## Common startup problems

| Symptom | Cause |
|---|---|
| `401` / `ERR_PNPM_FETCH_401` on install | `AuthToken` not exported, expired, or missing `read:packages` |
| `wrtc` fails to build or load | Missing toolchain packages; then `pnpm rebuild wrtc` and check `node -e "require('wrtc')"` |
| `AZURE_CACHE_FOR_REDIS_HOST_NAME is missing` | Redis vars absent — the client connects over TLS on port 6380 (`rediss://`) |
| `MONGO_DB_CONNECTION_STRING not found` | Mongo var absent |
| `503 Bot configuration not available` | Config service unreachable, or that `botId` doesn't exist |
| Socket clients get `Session ID unknown` | Only in multi-instance deployments — needs sticky sessions. Not a local issue. See [[chat-socket-events]] |
| Attachment/audio failures | `TEMP_DIR` missing or not writable. See [[attachments-media]] |

## Checklist
- [ ] `AuthToken` exported, `pnpm install` clean
- [ ] Env file filled from `.env.example`; never committed
- [ ] A real `botId`/`clientId` and reachable config service
- [ ] `pnpm run build` passes
- [ ] `GET /health` lists your bot
