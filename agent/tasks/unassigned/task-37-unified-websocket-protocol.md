# Task 37: Unified WebSocket protocol with typed message envelopes

**Milestone**: None (cross-cutting infrastructure)
**Design Reference**: None (design doc TBD — see Step 1)
**Estimated Time**: 3-5 days
**Dependencies**: None (but builds on the bugs surfaced by Task 34 + 35)
**Status**: Not Started
**Repositories**: `scenecraft-engine` (backend) + `scenecraft` (frontend)

---

## Objective

Replace the current per-feature WebSocket topology (three separate sockets: `/ws/`, `/ws/chat/:project`, `/ws/preview-stream/:project`) with a single long-lived WebSocket per client session that multiplexes typed messages across domains (chat, preview, jobs, etc.). Socket lifecycle decouples from feature lifecycle: opened once on session mount, closed once on unmount; feature state (play/pause/subscribe) rides on application-level messages.

---

## Context

The current design ties the preview-stream WebSocket's lifecycle to the `playing` boolean via `useMSEPlayback`. Every `playing` toggle tears down and reopens the socket, every `currentTime` tick nearly did the same (fixed in commit `b8ee701`). This produced a string of frustrating race-condition bugs:

- `generationRef` bumping inside `teardown()` invalidated the `sourceopen` callback that had *just* been set up, so the WebSocket never sent `play` (fixed in `b8ee701`)
- `currentTime` in the effect deps made Timeline's 60 Hz rAF loop continuously tear the socket down during playback (fixed in `b8ee701`)
- After those fixes, rapid play/pause still drops the `play` message: client teardown closes the old WS with code 1005, server handler returns and the *new* WS closes with code 1000, and the `action:play` that was queued during `CONNECTING` never makes it through

All of these flow from the same root: the socket is too tightly coupled to the feature's UI state. A persistent socket with message-level subscribe/unsubscribe fixes the class of bug, not just the current instance.

Secondary motivations:

- Chat, preview, and job-progress each have their own handler, auth guard, and reconnection logic. That triplication is already painful and will get worse as more real-time features land.
- Mobile/unstable networks need a single reconnecting pipe, not three racing to reconnect
- A typed envelope makes wire contracts obvious and testable; right now each socket has its own ad-hoc JSON shape

---

## Steps

### 1. Design the envelope

Write `agent/design/local.unified-websocket.md` covering:

- Envelope shape: `{ type: "<domain>.<action>", id?: string, payload: {...} }` for text frames
- Binary frames: first 4 bytes = channel id (u32 LE), remainder = payload (init segments, fMP4 fragments). Channel ids registered by subscribing text message.
- Canonical message types:
  - `chat.send`, `chat.receive`, `chat.typing`, `chat.error`
  - `preview.subscribe { project, channel }`, `preview.unsubscribe { channel }`, `preview.play { channel, t }`, `preview.pause { channel }`, `preview.seek { channel, t }`, `preview.error { channel, error }`
  - `job.subscribe`, `job.progress`, `job.complete`, `job.error`
- Request/response correlation via optional `id` field (client-generated UUID echoed in response)
- Error envelope: `{ type: "error", in_response_to?: id, error: { code, message } }`
- Auth: existing cookie works since one socket; no per-message auth
- Reconnection: client reconnects with exponential backoff; server replays last-known state for re-subscribed channels (design the replay semantics)

### 2. Backend: route dispatcher

In `scenecraft-engine`, replace the path-based routing in `ws_server.py._handle_connection` with a single entry point:

- Single handler accepts connection regardless of path (keep `/ws/` for compatibility during transition if needed)
- Parse incoming text frames as envelopes
- Maintain a per-connection dispatcher map: `{ "chat.send": handle_chat_send, "preview.play": handle_preview_play, ... }`
- Each handler gets `(ws, session, payload, envelope_id)` and returns a reply envelope or emits events asynchronously
- Preview binary fragments: wrap in channel-prefixed binary frames (no more raw fMP4 on the wire)

Keep `preview_worker.RenderCoordinator` as-is; only the transport changes. One RenderWorker per `(session, project, channel)`.

### 3. Backend: per-channel preview subscription

Rework `preview_ws.py` so preview state is per-channel-subscription, not per-connection:

- `preview.subscribe { project }` → allocates a channel id, spawns/fetches a worker, starts pump
- `preview.play/pause/seek { channel }` → dispatch to that worker
- `preview.unsubscribe { channel }` → pause worker, release channel
- Connection close → release all channels owned by this connection

### 4. Frontend: single `useWebSocket` context

In `scenecraft`, build a `WebSocketProvider` that:

- Opens one socket on app mount (or on first auth), closes on unmount
- Exposes `send(envelope)`, `request(envelope) → Promise<reply>`, `subscribe(type, handler) → unsubscribe`
- Handles reconnection with exponential backoff + state replay
- Replaces `useScenecraftSocket` (the job-progress hook), `openPreviewStream`, and whatever chat uses

### 5. Frontend: rewrite `useMSEPlayback`

Gut the WS lifecycle from the hook. It becomes:

- On mount: `preview.subscribe { project }` → returns `channel`
- On `playing=true`: `preview.play { channel, t }`
- On `playing=false`: `preview.pause { channel }`
- On explicit seek: `preview.seek { channel, t }`
- On unmount: `preview.unsubscribe { channel }`
- Binary `channel` frames are pushed to the SourceBuffer
- No more `generationRef`, no more teardown/restart dance

### 6. Migrate chat

Point the chat UI at the unified WS. Remove `/ws/chat/` backend handler.

### 7. Migrate job progress

Point job-progress consumers at the unified WS. Remove the default-path handler in `_handle_connection`.

### 8. Delete legacy

- Delete `preview_ws.py` once preview migrates; its logic moves into the dispatcher
- Delete `openPreviewStream` in `preview-client.ts`
- Delete `useScenecraftSocket.ts`
- Delete chat's custom WS wiring

### 9. Tests

- Unit: envelope parse/serialize round-trip
- Integration: server dispatches correctly across domains; channel binary frames don't cross-contaminate
- Regression: rapid play/pause does NOT cause fragment loss (this is the bug that motivated the task)
- Reconnection: kill + restore socket, verify subscriptions replay

---

## Verification

- [ ] Only one WebSocket connection visible in browser devtools per app session
- [ ] Rapid play/pause (≥5× in <1s) does not drop any `play` messages; fragments resume cleanly every time
- [ ] Preview, chat, and job progress all work over the same socket
- [ ] Killing the socket (server restart) → client reconnects within backoff window, resubscribes, preview playback resumes from where it was
- [ ] No `/ws/chat/`, `/ws/preview-stream/` paths still routed on the backend
- [ ] `npm run typecheck` and `pytest` clean on both repos
- [ ] Envelope shape documented in design doc; wire protocol obvious from reading the type definitions

---

## Key Design Decisions

### Motivation

| Decision | Choice | Rationale |
|---|---|---|
| Unify WS transport | Yes | Three concurrent WSes with hand-rolled lifecycle has produced a repeatable class of race-condition bugs (most recently: preview-stream dropping `play` on rapid toggle). One socket eliminates the class. |
| WS lifecycle | Per-session, not per-feature | Feature UI state (playing, chat-open, watching-job) should drive *messages*, not socket lifecycle. Socket stays open as long as the user session does. |
| Binary frame routing | 4-byte channel-id prefix | Preserves the low-overhead path for fMP4 fragments while still allowing multiplexing. Text envelopes register/unregister channels. |

### Scope

| Decision | Choice | Rationale |
|---|---|---|
| Backward compatibility | None — greenfield, no shims | Per user: "we dont need shims!!!!!! THIS IS A GREENFIELD PROJECT, NO FUCKING USERS". Cut over cleanly, delete legacy paths in the same series of commits. |
| Design doc | Required (Step 1) | Wire protocol is contract surface; write it down before implementing. |

---

## Notes

- This is a follow-up motivated by bugs found while shipping M11 tasks 34 + 35. Most of the "why" lives in those tasks' debugging history (commits `b8ee701`, `60e3233`).
- If this grows beyond a week, promote it to its own milestone.
- Don't start this until the current preview-stream pipeline is stable on its existing WS — this task replaces it, it doesn't supplement it.

---

**Repositories**: `scenecraft-engine` + `scenecraft`
**Estimated Completion Date**: TBD
