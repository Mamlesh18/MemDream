---
name: testing
description: Use when adding or running tests in this repo — where test files go so the existing mocha/nyc script finds them, what is worth unit testing first (pure renderers, parsers, key builders), what needs infrastructure and should be left alone, and the broken test/lint scripts.
---

# Testing

Related: [[git-workflow]], [[error-handling]].

## Current state — read this first

```
"test": "tsc --build && nyc mocha build/tests/**/*.test.js"
"lint": "tslint -c tslint.json 'src/**/*.ts'"
```

- **There are no test files.** The glob matches nothing, so `pnpm test` reports nothing useful today.
- **`pnpm lint` is broken**: `tslint.json` doesn't exist. The repo has `.eslintrc.js` (`standard` preset) instead. Fixing that means either restoring a tslint config or switching the script to eslint — a real decision, so raise it rather than changing it as a side effect.
- Installed and ready: `mocha` 10, `nyc` 17, `ts-node`, `@types/mocha`, `typescript` 5.7. **No assertion library** — use `node:assert/strict`, which needs no new dependency.
- `nyc` config lives in `package.json` (text reporter; excludes tests, `coverage`, `*.d.ts` and `src/index.ts`).

## Where tests go

The script runs compiled JS from `build/tests/`, and `tsc --build` compiles `src` → `build`. So:

```
src/tests/<unit-name>.test.ts      →  build/tests/<unit-name>.test.js
```

That's all it takes for `pnpm test` to start working. Name the file after the unit under test.

```ts
// src/tests/whatsappTemplateRenderer.test.ts
import assert from 'node:assert/strict';
import { renderTemplateContent } from '../utils/whatsappTemplateRenderer';

describe('renderTemplateContent', () => {
    it('substitutes body parameters in order', () => {
        const rendered = renderTemplateContent(
            { components: [{ type: 'body', parameters: [{ text: 'Asha' }, { text: '3pm' }] }] },
            { components: [{ type: 'BODY', text: 'Hi {{1}}, your slot is {{2}}' }] }
        );
        assert.equal(rendered.components[0].text, 'Hi Asha, your slot is 3pm');
    });
});
```

## Start with pure functions

These have no Mongo, Redis, HTTP or Bot Framework dependency, and each one encodes a rule that has already caused a bug:

| Unit | Why it's worth a test |
|---|---|
| `renderTemplateContent` (`utils/whatsappTemplateRenderer.ts`) | Body `{{n}}` order, button matching by index, each header format. See [[whatsapp-templates]] |
| `BroadcastReplyService.rebuildTemplate` | Placeholder slot order is a cross-repo contract: text and media consume a slot, button payloads don't. See [[broadcast-and-campaign]] |
| `EmailDedupService.buildClaimKey` | Must return `undefined` (fail open) with no identifiers, and the composite fallback otherwise. See [[email-channel]] |
| `cleanEmailBody`, `markdownToEmailHtml` | Quoted-chain stripping and HTML rendering |
| `generateFailureReason` (`utils/whatsapp-error.util.ts`) | Meta code → message mapping |
| `extractEmailAddresses`, `computeReplyCc` | The three address shapes (bare, list, display-name form) |
| Validators in `utils/validator.ts`, `getExtensionFromUrl` | Cheap, and used on untrusted input |

For a class method that only needs a logger or config, pass a minimal stub object rather than building the real dependency graph.

## What not to test yet

Don't try to unit test the routes, adapters, dialogs, middleware or services that need MongoDB, Redis, Meta or Azure. There is no integration harness, and mocking the whole Bot Framework stack costs more than it catches. If you want coverage there, propose a real integration setup (test Mongo/Redis, a fake channel) as its own piece of work.

## Conventions
- `describe` the unit, `it` a single behaviour phrased as an expectation.
- Arrange, act, assert. No network, DB, filesystem or clock dependence.
- Cover the edge that matters: the fail-open branch, the empty input, the wrong-order input.
- Never commit `.only`, and never `console.log` from a test.
- Don't add coverage thresholds until a baseline exists — a threshold on near-zero coverage just blocks everyone.

## Checklist
- [ ] File under `src/tests/`, named after the unit, ending `.test.ts`
- [ ] `pnpm test` runs it (`tsc --build` first)
- [ ] `node:assert/strict`, no new dependency unless discussed
- [ ] Pure unit, no infrastructure
- [ ] Asserts a documented rule, including its edge case
