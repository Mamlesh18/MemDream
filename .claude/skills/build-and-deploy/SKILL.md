---
name: build-and-deploy
description: Use when changing the Dockerfile or an Azure pipeline, debugging an image build, or releasing — the two-stage build and its native dependencies, the devtest/staging/prod/rcs-demo pipelines and their security gates, image tags, promotion between environments, and rollback.
---

# Docker build and pipelines

Related: [[local-setup]], [[security-checklist]], [[git-workflow]], [[debug-production]].

## The image

`Dockerfile`, two stages on `node:20-bookworm-slim`:

```
build stage:
  apt-get: python3 make g++ pkg-config cmake libasound2-dev libx11-dev libxkbcommon-dev
           libudev-dev libnss3-dev libssl-dev zlib1g-dev libglib2.0-dev ca-certificates git
  corepack enable → pnpm
  COPY .npmrc, package.json     ← copied first for layer caching
  pnpm config set allowed-build-dependencies wrtc
  npm i -g node-pre-gyp
  pnpm install
  COPY . .
  pnpm rebuild wrtc
  node -e "require('wrtc')"     ← smoke check: fails the build if the native module is broken
  pnpm run build

production stage:
  same apt-get toolchain
  COPY --from=build /usr/src/app .
  EXPOSE 3978 · CMD ["pnpm", "run", "start"]
```

Rules when editing it:
- Keep the `wrtc` rebuild and the `require('wrtc')` check. A silently broken `wrtc` means calls fail at runtime with no audio, which is far harder to diagnose than a failed build. See [[whatsapp-calling]].
- Keep `.npmrc` + `package.json` copied before the source, or every source change re-installs dependencies.
- Don't drop the toolchain from the production stage without testing `wrtc`, `sharp` and `ffmpeg-static` in the built image.
- Both stages set `ARG AuthToken` / `ENV AuthToken`. Because the **production** stage sets it as `ENV`, the token remains in the final image's config — anyone who can pull the image can read it. Don't propagate more secrets this way, and treat images as sensitive. See [[security-checklist]].
- `.dockerignore` excludes `.env` but **not** `.env.local`, `.env.devtest`, `.env.staging` or `.env.customerdev`. A local `docker build` bakes any of those that exist into the image. CI is unaffected (the agent clones from git, where they're gitignored).

## Pipelines

| Pipeline | Trigger branch | Stages | Tags |
|---|---|---|---|
| `azure-pipeline-devtest.yml` | `devtest` | Build + Push | 8-char commit SHA, `latest` |
| `azure-pipeline-staging.yaml` | `staging` | Build + Push | 8-char commit SHA, `latest` |
| `azure-pipeline-prod.yaml` | `main` | SecretScan → SonarQubeScan → SnykScan → Build → TrivyScan → Push | `$(Build.BuildNumber)`, `latest` |
| `azure-pipeline-rcs-demo.yaml` | `rcs-demo` | Build + Push | demo ACR |

All run on a self-hosted pool (`$(AgentName)`) and pass `--build-arg AuthToken=$(AuthToken)` from the environment's variable group. PRs never trigger them (`pr: branches: exclude: "*"`), and devtest/staging exclude their own pipeline file from the trigger paths.

**Only production has security gates:**
- **TruffleHog** filesystem secret scan — fails the pipeline on any finding.
- **SonarQube** with `qualitygate.wait=true` (300 s timeout) over `src`.
- **Snyk** SCA, `severityThreshold: high`, `failOnIssues: true`.
- **Trivy** image scan with `--exit-code 1` across all severities.

**Worth knowing:** the `Push` stage declares `dependsOn: Build`, not `TrivyScan`. So a failing Trivy scan does **not** block the push — the two stages run in parallel off `Build`. If the scan is meant to gate releases, `Push` needs `dependsOn: [Build, TrivyScan]`. Don't change it silently; raise it with whoever owns the pipeline.

## Deploy

The pipelines end at **push to ACR**. No deployment step exists in this repo — whatever runs the container (App Service or container platform) picks the image up. Before you promise a deploy time or a rollback path, confirm with whoever owns that configuration.

- Promotion is by merging `devtest → staging → main`, which rebuilds per environment. See [[git-workflow]].
- **Rollback** means redeploying a previous immutable tag (the commit SHA or build number), never `latest`.
- A new env var must be added to `.env.example` **and** to each environment's container/app settings. The app reads `process.env` at runtime, so a missing var shows up as a runtime failure, not a build failure.

## Checklist
- [ ] `wrtc` rebuild and smoke check intact
- [ ] Dependency layers still cached before the source copy
- [ ] No new secret added as `ARG`/`ENV`
- [ ] New env vars added to `.env.example` and to every environment's settings
- [ ] Image tag is immutable for anything you may need to roll back to
- [ ] Prod gate changes discussed, not silently altered
