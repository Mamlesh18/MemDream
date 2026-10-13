---
name: attachments-media
description: Use when handling attachments or media — downloading provider media, uploading to Azure Blob, audio transcription, image text extraction, appending extracted text to an activity, supported file types, or attachment size limits.
---

# Attachments and media

Related: [[channel-adapter]], [[llm-and-prompts]], [[api-endpoint]].

## Files
- `src/services/extract-attachment.service.ts` — inbound media → text
- `src/helpers/blobImageUrl.ts` — `uploadToAzure`, provider download helpers
- `src/services/azure-speech.service.ts` — audio transcription
- `src/services/image-extraction.service.ts` — image text extraction
- `SUPPORTED_ATTACHMENT_EXTENSIONS` / `SUPPORTED_ATTACHMENT_CONTENT_TYPES` in `src/enum/index.ts`

## Inbound: media becomes text

`extractAndStoreAttachments(context, whatsAppAdapter | null, botInstance)`:

1. **WhatsApp path** — read the media id from `channelData…messages[0]` by type (`audio`, `image`, `document`, `video`), then `whatsAppAdapter.client.DownloadMedia(mediaId)` (uses the bot's own token).
2. **Other channels** — `fetch(attachment.contentUrl)` → base64.
3. `audio` → `azureSpeechService.transcribeAudio(base64, 'en', messageId)`; `image` → `imageExtractionService.extractImageData(base64, contentType, messageId)`.
4. The result is **appended to `context.activity.text`**.

That last step is the important one: it's what lets a voice note or a photo of a receipt flow through intent matching and the dialog stack unchanged. Call it from the webhook **before** `bot.run(context)`, the way the RCS route does.

Unknown types return early without an error. Keep that — an unsupported attachment must not break the turn.

## Blob storage

```ts
const url = await uploadToAzure(buffer, `attachment/${uuid()}-${name}`, botId);
```

- Container is `<botId>-others`, created with **public blob read access**. Anything uploaded is readable by URL to anyone who has it. Never put non-customer-facing files, exports or credentials there.
- Always namespace the file name (a folder prefix plus a uuid), so uploads can't collide or be guessed.
- Re-host provider media in Blob and use that URL downstream. Provider URLs expire and need auth. See [[channel-adapter]].
- `downloadMediaForWhatsApp` in the helper uses the process-wide `FACEBOOK_ACCESS_TOKEN`, not the tenant's token. Prefer the adapter's `client.DownloadMedia` for anything per-tenant.

## Transcription and extraction
- **Speech**: model master by type `AUDIO_TRANSCRIPTION`, key from Key Vault, 25 MB cap, temp files under `TEMP_DIR` (default `./temp`) — anything written there must be cleaned up.
- **Image**: platform config `IMAGE-EXTRACTION` → model master → Azure OpenAI. Unlike `LlmService`, this one **throws** on misconfiguration, so callers must catch and continue without extracted text.
- Both cache their client and expose `resetClient()`, which the config callback calls. A new cached AI client must do the same. See [[multitenancy]] and [[llm-and-prompts]].

## Limits — always bound before you buffer

| Path | Limit |
|---|---|
| Email approve / compose | 25 MB per file, 10 files, memory storage |
| `/api/agent` base64 attachments | per-file and 50 MB total, checked from the base64 length **before** decoding |
| Audio transcription | 25 MB |

Validate the type against the supported enums (`getExtensionFromUrl` in `utils/validator.ts` helps) and reject early with `400`/`413`. Never decode a payload to find out how big it is.

## Rules
- Never persist base64 in Mongo. Store the blob URL, and strip `contentBase64` from activities before they're logged or saved.
- Never log attachment bytes, data URIs or file contents — log name, type and size. See [[logging]].
- Email attachments are the exception to re-hosting: they stay in memory, are sent, and are discarded. See [[email-channel]].

## Checklist
- [ ] Extraction runs before `bot.run`, appends to `activity.text`, and returns quietly for unsupported types
- [ ] Provider media re-hosted in Blob with a namespaced name
- [ ] Type validated against the supported enums
- [ ] Size bounded before buffering or decoding
- [ ] No base64 persisted or logged
- [ ] Temp files cleaned up; new AI clients expose `resetClient()`
