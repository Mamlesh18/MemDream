---
name: whatsapp-calling
description: Use when working on WhatsApp voice calling — inbound or outbound call flow, call webhooks, Socket.IO call events with the agent portal, WebRTC/SDP/ICE handling, the LiveKit AI/hybrid bridge, call state in Redis, call history writes, or reviewing any change to these files.
---

# WhatsApp calling

The highest-risk area of the repo. Failures are often **silent** (no audio, stuck calls, duplicate history). Read the related code before changing anything, and check every invariant below before finishing.

Related: [[data-access]], [[error-handling]], [[logging]].

## Map of the code

| Concern | File |
|---|---|
| HTTP routes (`/api/agent/call-permission`, `/api/agent/outbound-call`, `/api/call/handover`, `/api/ice-servers`), per-process call maps | `src/index.ts` |
| Webhook state machine (ringing, accept, reject, terminate, miss timer) | `src/services/call-event-handler.service.ts` |
| Agent portal Socket.IO events | `src/services/whatsapp-call-socket.service.ts` |
| Call id → bot routing | `src/services/voice-call-orchestrator.ts` |
| WebRTC sessions (base, inbound, outbound) | `whatsapp-call.service.ts`, `whatsapp-inbound-call.service.ts`, `whatsapp-outbound-call.service.ts` |
| AI/hybrid audio bridge | `src/services/livekit-audio-bridge.ts` |
| Cross-pod call state (meta, flags, locks, ended guard) | `src/redis/call-state.redis.ts` |
| Call history documents | `src/services/callhistory.service.ts` |
| Adapter | `src/custom-adapters/whatsapp-call/` |
| Design docs | `docs/hld-whatsapp-calling.md`, `docs/lld-whatsapp-calling.md`, `docs/hld-ai-voice-pipeline.md`, `docs/livekit-connectivity-and-mode-routing.md` |

Call settings come from `channelConfig.channels[WHATSAPP].configuration.whatsAppCall` (`getWhatsAppCallConfig` in `index.ts`). The inbound mode (human, AI or hybrid) decides whether a call goes to agents or LiveKit.

## Where state lives
- **Redis (`CallStateRedis`)** holds anything another pod may need: call meta, status flags, terminate data, the agent for a call, the phone lock, the call-ended guard, the summary-emitted guard. Keys are `call:<kind>:<callId>` with a 60-minute safety TTL.
- **In-process maps (`index.ts`)** hold only process-bound objects: `inboundCallMetadataMap`, `inboundCallTimerMap` (60s miss timers), `livekitBridges`, `botPhoneNumberIdMap` (cache).
- **Socket service sets** track portal-side progress: `activeInboundCallIds`, `pendingInboundCallIds`, `acceptingInboundCallIds`, `pendingAgentOffers`.
- New shared state goes in Redis. See [[data-access]].

## Invariants: check every change

### WebRTC
- [ ] `peerConnection.ontrack` is registered **before** `setRemoteDescription`. node-webrtc fires `ontrack` during SDP processing, so late registration silently misses the track.
- [ ] `RTCAudioSink` is created only after `ontrack` fired and the stream is set.
- [ ] ICE candidates that arrive before the remote description are buffered and flushed after `setRemoteDescription`.

### Audio to AI (LiveKit)
- [ ] Every `RTCAudioSink.ondata` handler forwards frames with `this.onUserAudioFrame?.(callId, data)`. If missing, the AI hears nothing and no error is thrown.
- [ ] `livekitBridges.get(callId)` is null-checked before use.
- [ ] After `bridge.stop()`, delete the entry from `livekitBridges` right away and clear `onUserAudioFrame`, so no frames go into a dead bridge.

### Termination and cleanup
- [ ] `clearTimeout(inboundCallTimerMap.get(callId))` runs **before** any history write or cleanup. Otherwise the miss timer can write BUSY/MISSED after REJECTED.
- [ ] Every exit path (ended, missed, rejected, cancelled, error) removes the call from **all** in-process maps and socket sets.
- [ ] History is written **before** `voiceOrchestrator.unregisterCall(callId)` and Redis `cleanupCall`. `finalizeCall()` in `call-event-handler.service.ts` does unregister + cleanup, so call it after the history write resolves.
- [ ] `registerCall(callId, botId)` happens before any socket event can route to the call.
- [ ] Duplicate or late terminate webhooks are safe: the call-ended guard (`markCallEnded`, atomic NX) is checked before writing.

### Socket events
- [ ] `agent-accept-call` handles both the pending path (WhatsApp not yet accepted: `onAgentAcceptPendingInboundCall`) and the already-accepted path.
- [ ] `onAgentAcceptPendingInboundCall` is **awaited**. Offers and ICE arriving meanwhile are buffered and replayed.
- [ ] `acceptingInboundCallIds` is cleared even when the accept throws (`finally`).
- [ ] AI → human handover uses `broadcastHandoverToAvailableAgents` with `isOutboundCall: false`. `true` makes the portal auto-accept.
- [ ] On failure, emit `call-ended` with a reason to the agent socket. Never leave the portal hanging.
- [ ] When adding an event, update the event list in the header comment of `whatsapp-call-socket.service.ts`.

### Call history
- [ ] Upserts: identity fields in `$setOnInsert`, mutable fields in `$set`, never the same or a parent/child path in both. `recipient` and `liveAgent` go **only** in `$set`.
- [ ] Optional fields use spread guards: `...(meta.callRejectedReason && { callRejectedReason: meta.callRejectedReason })`.
- [ ] A history write is never silently dropped when the UserThread is missing. Use the existing fallback.

### User state
- [ ] `USER_STATE.CALL_QUEUED` when broadcast to agents, `USER_STATE.CALL_AGENT` (with agentId) when accepted.
- [ ] Transitions use `getUserThreadModel().findOneAndUpdate(filter, { $set: { userState } })`.

## Errors and logging
- Use the call `ERROR_CODE` blocks: `WHATSAPP_CALL_*` (INL2601–), `WHATSAPP_INBOUND_CALL_*` (INL2621–), LiveKit (INL2633–). Check the enum for the next free number.
- Always include `callId` in log `details`. Customer phone goes only under a `customerPhoneNumber` key, which is masked. Never log SDPs or put phone numbers in message strings.

## Severity when reviewing
- **Blocker:** missing `onUserAudioFrame`, `ontrack` after `setRemoteDescription`, missing `clearTimeout` before cleanup, `$set`/`$setOnInsert` path conflict, unregister or cleanup before the history write.
- **High:** a map or set not cleaned on some exit path, wrong `isOutboundCall`, null written to optional fields, unchecked `livekitBridges.get`.
- **Medium:** missing `callId` in logs, portal not told on failure.
