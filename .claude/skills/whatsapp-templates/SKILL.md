---
name: whatsapp-templates
description: Use when working with WhatsApp message templates — sending or rendering a template, template components and placeholders, fetching approved templates from Meta, WhatsApp delivery status webhooks and wamid mapping, or Meta template error codes.
---

# WhatsApp templates and status webhooks

Templates are needed whenever the bot speaks first: broadcasts, proactive messages outside the 24-hour window, agent mention notifications, and call permission requests.

Related: [[channel-adapter]], [[proactive-messaging]], [[notifications]], [[broadcast-and-campaign]].

## Files
- `src/utils/whatsappTemplateRenderer.ts` — `renderTemplateContent(payload, definition)`
- `src/custom-adapters/cloudapi-whatsapp/WhatsAppMetaCloudClient.ts` — send + template fetch and cache
- `src/middleware/logger.middleware.ts` — renders templates for persistence, `updateMessageRenderedTemplate`
- `src/utils/whatsapp-error.util.ts` — `generateFailureReason`, `WHATSAPP_ERROR_MESSAGES`
- `handleWhatsAppStatusUpdate` / `processWhatsAppStatusAsync` in `src/index.ts`

## Sent payload vs approved definition

Two different shapes, and rendering needs both:
- **Payload** — what we send Meta: `components[]` with `parameters[]` (`body`, `button` by `index`, `header` text or media).
- **Definition** — the approved template from Meta: `components[]` with `text` containing `{{1}}`, `format` (TEXT/IMAGE/VIDEO/DOCUMENT/LOCATION/GIF), and `buttons[]`.

`renderTemplateContent` merges them so the portal shows real text rather than `{{1}}`:
- BODY: `{{n}}` replaced by the nth body parameter, in order.
- HEADER: text replaces `{{1}}`; media takes `link` or `id` as `url`; DOCUMENT also carries `filename`; GIF arrives as a video parameter.
- BUTTONS: `URL` substitutes `{{1}}` in the url, `QUICK_REPLY` takes `payload`, `PHONE_NUMBER` keeps its number, `COPY_CODE` takes the code — matched **by button index**.
- Unknown component types (`CALL_PERMISSION_REQUEST`, `LIMITED_TIME_OFFER`, …) pass through as `{ type }` only.

Adding support for a new component type means editing **both** the renderer and the send path.

## Placeholder order
Body parameters map positionally to `{{1}}`, `{{2}}`, …; buttons map by component `index`; the header takes a single parameter. Parameter count must match the approved definition exactly, or Meta rejects the send with `132005` (parameter mismatch). Broadcast replies have their own ordering contract — see [[broadcast-and-campaign]].

## Fetching approved templates
The client caches `wabaId → templates` from Graph `/{wabaId}/message_templates` (fields `name,status,parameter_format,components`, limit 250) in a **module-level map with a timestamp**. That cache is per pod, so a newly approved template can be missing until it expires or the pod restarts — worth knowing when a tenant says "the template exists but the bot says it doesn't".

Template names always come from config (channel config, bot config, notification settings). Never hardcode one.

## Status webhooks

Route order in `POST /api/whatsapp/messages/:botId` is deliberate:

```
1. callEventHandler.handleCallWebhookEvent   ← first: a call webhook can also carry value.statuses
2. handleWhatsAppStatusUpdate                ← value.statuses
3. handleCallPermissionReply
4. seedBroadcastReplySession → whatsAppAdapter.processActivity
```

Don't reorder. Status handling matches on `value.statuses` alone and would otherwise swallow call webhooks.

`handleWhatsAppStatusUpdate` acks `200` immediately and then runs `processWhatsAppStatusAsync(body, botId).catch(log)`. Meta retries aggressively, so the ack must not wait on persistence — but that means **every** failure afterwards is log-only. Keep the `.catch`, and keep `Modules.TRANSCRIPT_PROVIDER` with `WHATSAPP_STATUS_LOG_FAILED`.

## wamid
Outbound messages are matched to Meta's message id (`wamid`) with `updateMessageWithWhatsAppId` / `findMessageByWhatsAppId`, and the portal is told through `handleWamidUpdate` → `updateWhatsappMessageId`. Status updates (sent, delivered, read, failed) are keyed off that id — so a message with no wamid recorded can never show a delivery state. See [[chat-socket-events]].

## Errors
`generateFailureReason(error)` turns a Meta error into readable text. Template codes worth recognising: `132000` not found · `132001` format invalid · `132005` parameter mismatch · `132012` paused for quality · `132015` disabled · `132016` pending approval. Add new codes to `WHATSAPP_ERROR_MESSAGES`, not inline.

## Never log
Rendered template bodies, parameter values or recipient numbers — they're customer data. Log the template name, status and error code. See [[logging]].

## Checklist
- [ ] Template name from config
- [ ] Parameter count and order match the approved definition
- [ ] New component types handled in both renderer and send path
- [ ] Webhook branch order unchanged; status processing stays async after the ack
- [ ] wamid recorded so delivery status can attach
- [ ] Meta error codes mapped in `WHATSAPP_ERROR_MESSAGES`
