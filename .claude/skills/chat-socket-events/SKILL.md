---
name: chat-socket-events
description: Use when adding or changing Socket.IO events between the backend and the agent portal (ChatGateway) — rooms, handshake identity, event payloads, emitting to a bot or agent, cross-pod delivery with the Redis adapter, or debugging why the portal doesn't receive an event.
---

# ChatGateway socket events

`src/services/chatGateway.service.ts` is the real-time contract with the agent portal. Changing an event name or payload is a **breaking API change** for the frontend.

Call-related events live in a different service — see [[whatsapp-calling]].

## Setup
- One `ChatGateway` per process (singleton on the HTTP server). Get it with `ChatGateway.getInstance()`; services use `ChatGateway.getInstance()?.handleX(...)`.
- `@socket.io/redis-adapter` with dedicated pub/sub Redis clients propagates emits across pods. Both are closed in `shutdown()`.
- Polling transport needs **sticky sessions** (ARR affinity on Azure App Service), or clients get `Session ID unknown`.
- `SOCKET_DEBUG=true` turns on join and emit logging.

## Identity and rooms

A handshake middleware reads `socket.handshake.auth` and stores `botIds`, `aosIds` and `agentId` on `socket.data`. On connect the socket joins:

| Room | Who is in it |
|---|---|
| `bot:<botId>` | Every agent socket of that bot (one agent can be in many bot rooms) |
| `aosId:<aosId>` | Agents for one Area of Speciality |
| `agent:<agentId>` | One agent |
| `<conversationId>` | Clients watching one conversation, joined via the `joinRoom` event |

Identity is **fail-open**: a socket with no identity still connects, but receives no room-scoped events.

## Events

**Client → server**

| Event | Effect |
|---|---|
| `joinRoom` (room) | Joins it, acks `joinedRoom` |
| `leaveRoom` (room) | Leaves it, acks `leftRoom` |
| `messageToServer` | Legacy: broadcasts `messageToClient` to **everyone**. Don't build on it, and don't add new global events |

**Server → client** (each method validates its payload, logs and returns on invalid input)

| Method | Emits | To |
|---|---|---|
| `handleMessageToRoom` | `handleMessageToRoom`, `newMessage`, `updateConversations` | bot room, conversation room, bot room |
| `handleWamidUpdate` | `handleWamidUpdate`, `newMessage`, `updateWhatsappMessageId` | bot room, conversation room, bot room |
| `handelUserLeftConversation` | `userLeftConversation` | bot room |
| `handleUserRequested` | `userRequested` | `aosId:<id>` if present, else bot room |
| `handleEmailConversation` | `emailConversation` | bot room + conversation room |
| `handleEmailDraftPending` | `emailDraftPending` | bot room |
| `handleWhatsAppStatusUpdate` | `whatsAppStatusUpdate` | bot room |
| `handleCallPermissionUpdate` | `callPermissionUpdate` | bot room |
| `handleMentionDiscussion` | `discussion` | bot room |
| `handleMessageToClients` | `messageToClient` | everyone (legacy) |

## Rules
- **Never `io.emit` tenant data.** Always `this.io.to('bot:' + botId)` or a more specific room. A global broadcast hands one tenant's message bodies and ids to every connected socket, including other tenants'.
- Validate the payload first (`botId` plus the id the event is about), `console.error` and return on invalid input. Emitting is not allowed to throw into the caller.
- Payload types are declared as interfaces at the top of the file (`IMessageToRoomPayload`, `IEmailConversationPayload`). Add new fields there, never as loose `any` extras.
- Keep a payload a superset when a channel needs more: email extends the generic payload with `status`, `subject` and `isPaused`.
- **Email has its own event on purpose.** Don't route email through `handleMessageToRoom`: a `DRAFT` must never look like a message the customer already received.
- Emit after persistence, so the portal never shows a message that isn't saved.
- The process-local `clients` Set and `adapter.rooms.get(room).size` count **this pod only**. Never use them for cluster-wide counts or for "is anyone listening" decisions.
- Adding or renaming an event needs frontend coordination — say so in the PR, and roll out server-first (new event added while the old one keeps firing).
- Use `logRoomEmit(event, room)` for new emits so debugging stays consistent.

## Checklist
- [ ] Room-scoped, never `io.emit`
- [ ] Payload validated, and typed in the interface
- [ ] Emitted after the data is persisted
- [ ] No per-pod counts treated as global
- [ ] Frontend change noted in the PR; old event kept during rollout
