# Spec: Engine Chat Pipeline — Internals (Streaming, History, Dispatcher, Interrupt)

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Active (retroactive — refactor-regression spec for `chat.py`)

---

## Purpose

Pin down the **engine-side pipeline internals** of the scenecraft chat subsystem — the moving parts inside `scenecraft-engine/src/scenecraft/chat.py` (5,594 LOC) that a refactor (split / extract / restructure) MUST preserve bit-for-bit at the observable boundary.

This spec is the **regression contract** for a refactor that may:

- split `chat.py` into multiple modules (e.g. `chat/pipeline.py`, `chat/history.py`, `chat/dispatcher.py`, `chat/system_prompt.py`, `chat/elicitation.py`, `chat/tools.py`);
- collapse the ~150-line `if/elif` chain in `_execute_tool` into a dispatch-table;
- extract the WS message-switch out of `handle_chat_connection`;
- replace the streaming loop body with a class — provided none of the observable behavior in this document changes.

It does NOT redefine the tool-dispatch/elicitation **contract** — that lives in
`scenecraft/agent/specs/local.chat-tool-dispatch-and-elicitation.md` and is
referenced from this spec as the layer ABOVE. This spec is the **layer BELOW**:
how the engine actually assembles the stream, manages history, connects MCP,
drives the tool loop, and handles interruption.

## Source

- **Mode**: `--from-draft` (retroactive; codifies observed pipeline internals)
- **Primary sources** (read):
  - `scenecraft-engine/src/scenecraft/chat.py`:
    - Lines 27–100 — `_add_message`, `_get_messages` (SQLite `chat_messages` persistence, 50-row window)
    - Lines 106–225 — `_build_system_prompt` (dynamic per-stream, DB-backed context)
    - Lines 1429–1530 — `TOOLS` list (34 built-ins), `_DESTRUCTIVE_TOOL_PATTERNS`, `_DESTRUCTIVE_TOOL_ALLOWLIST`, `_is_destructive`
    - Lines 1770–1796 — `_recv_elicitation_response` (futures dict, 300s timeout, auto-decline)
    - Lines 4872–4922 — `_await_generation_job` (job polling + `tool_progress` frames)
    - Lines 4924–5149 — `_execute_tool` (plugin-namespaced branch + built-in if-chain)
    - Lines 5199–5307 — `handle_chat_connection` (WS connect, single-reader loop, MCP bridge kickoff, stream halt)
    - Lines 5309–5582 — `_stream_response` (history load, system prompt, tool merge, 10-iter loop, cancellation persist)
  - `scenecraft-engine/src/scenecraft/mcp_bridge.py` (214 LOC, full) — `MCPBridge.connect` + `all_tools` + `has_tool` + `call_tool` + `close`
  - `scenecraft-engine/src/scenecraft/chat_generation.py` (referenced only) — origin of the job ids `_await_generation_job` polls
- **Contract spec (above this layer)**: `../scenecraft/agent/specs/local.chat-tool-dispatch-and-elicitation.md`
- **Audit**: audit-2 §1B (chat pipeline) + §3 leaks #19, #20, #21

---

## Scope

### In scope (engine internals the refactor MUST preserve)

- **Persistence schema + access**: `chat_messages(id, user_id, role, content, images, tool_calls, created_at)` SQLite table; `_add_message` insert + return shape; `_get_messages(..., limit=50)` ordering semantics (DESC by id, then reversed).
- **History window**: exactly the last 50 rows for the `user_id`, oldest-first after reverse, passed to `_history_to_claude_messages`.
- **System prompt assembly**: `_build_system_prompt(project_dir, project_name)` recomputes on every `_stream_response` call; reads `keyframes`, `transitions`, `tracks` counts + `meta` key/values; embeds them in a multi-line string; never cached.
- **Tool catalog shape**: `{name, description, input_schema}` — exactly those three keys — for every entry in `tools_for_claude`.
- **Tool catalog merge order**: `list(TOOLS) + plugin_contributed + mcp_tools`, re-materialized **once per `_stream_response` call** and reused across the 10-iteration loop.
- **Dispatcher precedence inside `_execute_tool`**: `"__"` in name ⇒ `PluginHost.get_mcp_tool(name)` first; else the built-in if/elif chain; else `{"error": "unknown tool: ..."}`.
- **Dispatcher precedence in `_stream_response`**: `bridge.has_tool(name)` ⇒ `bridge.call_tool(...)`; else `_execute_tool(...)`. (Plugin names take the `__` branch inside `_execute_tool` only if the bridge does not claim them first — see OQ-2.)
- **Destructive classifier (3 layers)**: allowlist (wins) → plugin `destructive:bool` (if `__` name + registered plugin tool) → substring patterns on lowercased name.
- **Elicitation mechanism**: `asyncio.Future` created by `_recv_elicitation_response`, registered in the shared `elicitation_waiters: dict[str, asyncio.Future]`, awaited with `asyncio.wait_for(..., timeout=300)`, popped in `finally`.
- **Timeout semantics**: `asyncio.TimeoutError` → `return "decline"`; `asyncio.CancelledError` → re-raise (never swallowed); any action != `"accept"` → treated as decline by the caller.
- **Streaming loop** (`_stream_response`): 10-iteration cap; exit on `stop_reason != "tool_use"` OR zero `tool_use` blocks this turn; `text_delta` → `chunk`; `content_block_start` of `tool_use` → exactly one `tool_call` per tool id (tracked via `announced_tool_ids: set`); final.content drives accumulation.
- **MCP bridge integration**: `MCPBridge()` instantiated per connection; `connect("remember", user_id=...)` fired as a background task (never awaited by the read loop); `bridge.all_tools()` returns `[]` until the background task succeeds; degraded mode — if the connect fails, chat still works with built-ins + plugin tools.
- **Connection handler**: single-reader `async for raw in ws:` loop; only frame-type switch for `message` / `elicitation_response` / `stop` / `ping`; `current_stream: asyncio.Task | None` holds the in-flight task; `_halt_current_stream()` is idempotent (no-op if task is None or done).
- **Interrupt path**: on `asyncio.CancelledError` inside `_stream_response`, append `streamed_text_this_turn` to `all_blocks`, persist via `_add_message` (tolerating failure), emit `message` with `interrupted:true`, emit `halted` with `reason:"interrupted_by_user"`, emit `complete`, re-raise `CancelledError`.
- **Error path**: on `anthropic.APIError` or other exception, emit `error` + `complete` (NOT `halted`).
- **Hardcoded model config**: `model="claude-sonnet-4-20250514"`, `max_tokens=4096` (both literal in `_stream_response`).
- **`tool_progress` origin**: `_await_generation_job` is the ONLY emitter of `tool_progress` frames; it polls `job_manager.get_job(job_id)` every `poll_interval=0.5`; forwards on `completed` counter change only.

### Out of scope (owned by other specs or flagged)

- **Event names / destructive precedence / elicitation shape** — owned by the contract spec `local.chat-tool-dispatch-and-elicitation.md`. This spec references it; if the two disagree, the contract spec wins.
- **Per-tool handler logic** — each `_exec_*` function (e.g. `_exec_update_keyframe_prompt`) is specced by its feature's spec or is an internal DB helper.
- **Job manager + `/ws/jobs` bus** — `local.job-manager-and-ws-events` (planned). Only `_await_generation_job` is in scope here as the consumer.
- **Frontend rendering** — owned by the contract spec + chat-panel spec.
- **OAuth token store / `SERVICES` map** — used by `MCPBridge.connect`, treated as an opaque provider.
- **`_history_to_claude_messages` block-splitting logic** — owned by the contract spec (R3).
- **WS event namespacing (`core__chat__*` vs bare types)** — per the contract spec, INV-4 mandates namespaced events; the current code emits bare `"chunk"`/`"tool_call"`/etc. This is a known discrepancy. The refactor MAY change the wire names iff done jointly with the contract spec update. (Not regressed by this spec — both shapes are accepted as long as they match what the frontend expects at the time of refactor.)

---

## Interfaces / Data Shapes

### `chat_messages` table (SQLite)

```
CREATE TABLE chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    role        TEXT    NOT NULL,        -- 'user' | 'assistant'
    content     TEXT    NOT NULL,        -- plain string OR JSON-stringified blocks
    images      TEXT,                    -- JSON-stringified list[str] | NULL
    tool_calls  TEXT,                    -- JSON-stringified list[dict] | NULL
    created_at  TEXT    NOT NULL         -- ISO-8601 UTC
);
```

### `_add_message(...) -> dict` return shape

```python
{
    "id":         int,
    "user_id":    str,
    "role":       "user" | "assistant",
    "content":    str | list[dict],   # decoded to list if stored as JSON blocks
    "images":     list[str] | None,
    "created_at": str,                 # ISO-8601 UTC
}
```

### `_get_messages(project_dir, user_id, limit=50) -> list[dict]`

- Selects `id, user_id, role, content, images, tool_calls, created_at` `ORDER BY id DESC LIMIT <limit>` on `chat_messages WHERE user_id = ?`.
- Reverses in Python so callers get **oldest-first**.
- Decodes `content` to `list[dict]` **only for `role == "assistant"`** and only when the JSON parses to a list.
- Attaches `images` / `tool_calls` as parsed JSON lists when the column is non-null.

### System prompt (`_build_system_prompt(project_dir, project_name) -> str`)

- Freshly computed on every `_stream_response` call — never cached, never memoized.
- Reads four things from `get_db(project_dir)` (synchronous, in-process):
  - `SELECT COUNT(*) FROM keyframes WHERE deleted_at IS NULL` → `kf_count`
  - `SELECT COUNT(*) FROM transitions WHERE deleted_at IS NULL` → `tr_count`
  - `SELECT COUNT(*) FROM tracks` → `track_count`
  - `SELECT key, value FROM meta` → `meta` dict, with defaults `fps="24"`, `resolution="1920,1080"`, `title=<project_name>`
- Returns a multi-line f-string embedding those counts + a long human-readable tool catalog description.

### Tool catalog entry (advertised to Claude)

```python
{"name": str, "description": str, "input_schema": dict}   # exactly these three keys
```

### Tool catalog sources (merged, in order)

1. `TOOLS` — module-level list in `chat.py`, **34 built-in entries** (see contract spec R2).
2. `PluginHost.list_mcp_tools()` — plugin tools; each entry's `name = t.full_name` = `{plugin_id}__{tool_id}`.
3. `bridge.all_tools()` — external MCP tools (may be `[]` if the bridge hasn't connected yet).

Merge happens **once per `_stream_response` call**, at the top, before the 10-iter loop. The list is NOT rebuilt per outer iteration.

### `_execute_tool(project_dir, name, input_data, *, ws, tool_use_id, project_name) -> tuple[dict, bool]`

- Plugin-namespaced branch (`"__" in name`):
  - `PluginHost.get_mcp_tool(name)` — if non-None:
    - Build `context = {"project_dir": Path, "project_name": str|None, "ws": ServerConnection|None, "tool_use_id": str|None}`.
    - Call `handler(input_data, context)`. Any exception ⇒ `({"error": f"{type(exc).__name__}: {exc}"}, True)`.
    - Non-dict return ⇒ `({"error": f"plugin tool {name!r} returned non-dict: {type(result).__name__}"}, True)`.
    - Dict return ⇒ `(result, "error" in result)`.
  - `PluginHost.get_mcp_tool(name) is None` (stale name, plugin unloaded): falls through to built-in switch, which won't match, ⇒ `({"error": "unknown tool: {name}"}, True)`.
- Built-in branch: large `if name == "..."/elif name == "..."` chain (~150 lines) routing to `_exec_*` helpers or `_await_generation_job`.
- Unknown: `({"error": f"unknown tool: {name}"}, True)`.

### `_recv_elicitation_response(waiters, elicitation_id, timeout=300) -> "accept" | "decline"`

- Creates `fut = loop.create_future()`, registers `waiters[elicitation_id] = fut`.
- `await asyncio.wait_for(fut, timeout=300)`:
  - `asyncio.TimeoutError` → logs, returns `"decline"`.
  - `asyncio.CancelledError` → re-raised (NOT swallowed).
- `finally`: `waiters.pop(elicitation_id, None)`.
- Normalizes result: `"accept"` if action == `"accept"`, else `"decline"`.

### `_stream_response(ws, project_dir, project_name, user_id, bridge, elicitation_waiters)` state variables

- `all_blocks: list[dict]` — persisted across the 10-iter loop; final assistant content.
- `tool_calls_log: list[dict]` — per-tool rows for the `tool_calls` column.
- `announced_tool_ids: set[str]` — dedupes `tool_call` events per tool_use id.
- `streamed_text_this_turn: str` — rolling buffer of `text_delta`s for the current outer iteration; cleared at the top of each iteration AND after `final` materializes; flushed into `all_blocks` only on `CancelledError`.

### `handle_chat_connection` state

- `bridge: MCPBridge` — per-connection.
- `current_stream: asyncio.Task | None` — at most one in-flight stream.
- `elicitation_waiters: dict[str, asyncio.Future]` — shared with `_stream_response`.

### `_await_generation_job(ws, tool_use_id, project_name, job_id, poll_interval=0.5, timeout=900)`

- Polls `job_manager.get_job(job_id)` every `0.5s`.
- On `completed` counter change: emits `{type:"tool_progress", toolProgress: {id, phase:"generating", pct, message}}` (send failure swallowed).
- Terminal:
  - `status == "completed"` → `(job.result or {}, False)`.
  - `status == "failed"` → `({"error": job.error or "generation failed"}, True)`.
  - Timeout (900s wall clock): `({"error": "generation job {job_id} did not finish within 900s; it may still be running"}, True)`. **Underlying job is NOT cancelled.**

---

## Requirements

### R-Persistence (history / DB)

1. **R1 — Table schema is load-bearing**. `chat_messages` MUST have the columns `(id, user_id, role, content, images, tool_calls, created_at)`. `images` and `tool_calls` MUST be nullable TEXT storing JSON. Refactors MUST NOT rename or drop columns without a migration.
2. **R2 — `_add_message` insert shape**. Every call MUST `INSERT` exactly one row with `created_at = datetime.now(timezone.utc).isoformat()` and return the `{id, user_id, role, content, images, created_at}` dict, with `content` decoded back to a list when the stored value parses to a JSON list.
3. **R3 — `_get_messages` ordering**. MUST return at most `limit` rows, filtered by `user_id`, in **oldest-first** order (select DESC by id + reverse in Python). Default `limit=50`. `role == "assistant"` content that parses to a JSON list MUST be decoded; user rows MUST NOT be decoded even if the content looks like JSON.
4. **R4 — 50-message window**. `_stream_response` MUST call `_get_messages(..., limit=50)`. No larger window, no smaller.

### R-System prompt

5. **R5 — Dynamic per-call**. `_build_system_prompt` MUST be called once per `_stream_response` invocation (NOT cached between calls). It MUST re-read the four DB facts on every call.
6. **R6 — Default meta values**. Missing `meta` keys MUST fall back to `fps="24"`, `resolution="1920,1080"`, `title=project_name`.
7. **R7 — Soft-delete respected in counts**. `keyframes` and `transitions` counts MUST exclude rows where `deleted_at IS NOT NULL`. `tracks` has no soft-delete — counted unconditionally.

### R-Tool catalog

8. **R8 — Three-key shape**. Every entry in `tools_for_claude` MUST have exactly the keys `{name, description, input_schema}` — no extras, no missing.
9. **R9 — Merge order**. `tools_for_claude = list(TOOLS) + plugin_contributed + mcp_tools`, in that order. Built-ins first, then plugin tools, then bridge tools.
10. **R10 — Materialized once per stream**. The merged list MUST be built once at the top of `_stream_response` and reused across the 10-iteration loop. Refactors MUST NOT rebuild it per outer iteration.
11. **R11 — Plugin entry derivation**. Plugin entries MUST use `t.full_name` as `name` (= `{plugin_id}__{tool_id}`), with `description = t.description` and `input_schema = t.input_schema`, straight through from `PluginHost.list_mcp_tools()`.
12. **R12 — Bridge entries pass-through**. `bridge.all_tools()` entries MUST be forwarded unchanged.

### R-Destructive classifier

13. **R13 — Allowlist wins**. If the lowercased name is in `_DESTRUCTIVE_TOOL_ALLOWLIST`, `_is_destructive` MUST return `False` without further checks.
14. **R14 — Plugin flag wins over patterns**. If `"__" in name` AND `PluginHost.get_mcp_tool(name)` is non-None, the plugin's `destructive: bool` MUST be returned. `PluginHost` lookup failures (exceptions) MUST fall through to the pattern check (not propagate).
15. **R15 — Substring patterns (fallback)**. Else return `True` iff any substring in `_DESTRUCTIVE_TOOL_PATTERNS` appears in the lowercased name.

### R-Dispatcher

16. **R16 — Plugin-namespaced priority in `_execute_tool`**. If `"__" in name`, `PluginHost.get_mcp_tool(name)` is consulted FIRST. A match dispatches via the plugin handler; a miss falls through to the built-in switch.
17. **R17 — Bridge priority in `_stream_response`**. In the per-tool-use dispatch branch, `bridge.has_tool(name)` is checked BEFORE `_execute_tool`. A match dispatches via `bridge.call_tool`; a miss goes to `_execute_tool`.
18. **R18 — Unknown tool error shape**. `_execute_tool` with a name that matches no plugin tool AND no built-in MUST return `({"error": f"unknown tool: {name}"}, True)`.
19. **R19 — Plugin handler error wrapping**. Exceptions from a plugin handler MUST be caught and returned as `({"error": f"{type(exc).__name__}: {exc}"}, True)`. Non-dict returns MUST be returned as `({"error": f"plugin tool {name!r} returned non-dict: {type(result).__name__}"}, True)`.
20. **R20 — Context dict shape**. Plugin handlers MUST receive `context = {"project_dir": Path, "project_name": str|None, "ws": ServerConnection|None, "tool_use_id": str|None}` — exactly these four keys.
21. **R21 — Input coercion**. `input_data = input_data or {}` at the top of `_execute_tool` — `None` inputs MUST become `{}` before any dispatch branch.

### R-Elicitation mechanism

22. **R22 — Futures-dict pattern**. Elicitation blocking MUST use a single shared `elicitation_waiters: dict[str, asyncio.Future]`, populated by `_recv_elicitation_response` and resolved by the `handle_chat_connection` read loop. `_stream_response` MUST NOT call `ws.recv()` directly.
23. **R23 — 300s timeout**. `asyncio.wait_for(fut, timeout=300)` with `TimeoutError` → `return "decline"`. The literal `300` MUST be preserved (refactor may extract to a named constant but not change the value without explicit spec update).
24. **R24 — CancelledError re-raised**. `asyncio.CancelledError` inside `_recv_elicitation_response` MUST be re-raised; NEVER swallowed as a decline.
25. **R25 — Finally pops waiter**. The waiters dict entry MUST be popped in a `finally` block, regardless of outcome (accept, decline, timeout, cancel).
26. **R26 — Action normalization**. Any action value that is not exactly the string `"accept"` MUST be normalized to `"decline"`.

### R-Streaming loop

27. **R27 — 10-iteration cap**. The outer loop MUST be `for _ in range(10)`. Exit conditions: (a) `final.stop_reason != "tool_use"`; (b) `turn_tool_uses` is empty; (c) counter reaches 10. No other exit.
28. **R28 — `tool_call` de-duplication**. Exactly one `tool_call` event per tool_use id per stream, enforced via `announced_tool_ids: set[str]`. Malformed streams that replay `content_block_start` for the same id MUST NOT produce duplicate events.
29. **R29 — `chunk` emission**. Every `content_block_delta` with `type == "text_delta"` MUST emit a `chunk` event with the delta text AND append to `streamed_text_this_turn`.
30. **R30 — Buffer reset semantics**. `streamed_text_this_turn` MUST be cleared (a) at the top of each outer iteration, AND (b) immediately after `final = await stream.get_final_message()` resolves. It MUST NOT accumulate across iterations.
31. **R31 — Message feed-back**. After executing a turn's tool uses, `_stream_response` MUST append `{"role":"assistant", "content":[_block_to_dict(b) for b in final.content]}` then `{"role":"user", "content":tool_result_blocks}` to `messages` before looping.
32. **R32 — Model + token config**. `client.messages.stream(model="claude-sonnet-4-20250514", max_tokens=4096, system=..., messages=..., tools=...)` — both literals MUST be preserved. (Model upgrades are explicit spec changes, not silent refactors.)

### R-MCP bridge integration

33. **R33 — Lazy connect**. `bridge.connect("remember", user_id=user_id)` MUST be fired via `asyncio.create_task(...)` — NEVER awaited in the hot path of `handle_chat_connection`.
34. **R34 — Connect failure is soft**. If the background connect fails (no token, network error, no SDK), the WS loop MUST continue; `bridge.all_tools()` MUST return `[]`; built-ins + plugin tools MUST still be available.
35. **R35 — Connect success mid-session**. If the background connect succeeds AFTER the first `_stream_response` call, the new tools become visible on the NEXT `_stream_response` call (because the merge happens per-call). Emitting this "tools appeared" surface mid-session is intentional (see OQ-5).
36. **R36 — `bridge.close()` on teardown**. `handle_chat_connection`'s `finally` MUST call `await bridge.close()` inside a `try/except` (errors logged, not propagated).

### R-Connection handler

37. **R37 — Single-reader loop**. Only `handle_chat_connection`'s `async for raw in ws:` loop reads WS frames. Refactors MUST NOT introduce concurrent `ws.recv()` calls.
38. **R38 — Frame-type switch**. Recognized types: `"message"`, `"elicitation_response"`, `"stop"`, `"ping"`. Unknown types MUST be silently ignored (no error, loop continues).
39. **R39 — Invalid JSON**. `json.JSONDecodeError` from `json.loads(raw)` MUST produce `{type:"error", error:"Invalid JSON"}` and continue. The connection MUST NOT be closed.
40. **R40 — Halt-before-stream**. A new `message` frame MUST await `_halt_current_stream()` BEFORE persisting the user row and spawning the next stream task.
41. **R41 — Empty content ignored**. A `message` frame with empty `content.strip()` MUST be silently dropped (no persist, no stream).
42. **R42 — Stop semantics**. A `stop` frame MUST `_halt_current_stream()` but MUST NOT spawn a new stream.
43. **R43 — Ping/pong**. A `ping` frame MUST reply `{type:"pong"}`.
44. **R44 — At-most-one stream**. `current_stream` MUST hold at most one non-done task at any time. `_halt_current_stream` is idempotent when the task is None or already done.

### R-Interrupt path

45. **R45 — Partial buffer flush**. On `asyncio.CancelledError` inside `_stream_response`, if `streamed_text_this_turn` is non-empty, it MUST be appended to `all_blocks` as a `{"type":"text", "text":...}` block BEFORE persistence.
46. **R46 — Conditional persist**. Persistence on cancel runs iff `all_blocks or tool_calls_log` is truthy. An empty cancel (no text, no tools) persists nothing.
47. **R47 — Persist failure is soft**. `_add_message` raising during the cancel path MUST be logged and swallowed (NOT propagated). The `CancelledError` MUST still be re-raised after persistence attempts.
48. **R48 — `interrupted:true` flag**. The emitted `message` frame on cancel MUST have `message.interrupted = True`.
49. **R49 — `halted` + `complete`**. On cancel, after persist + message emission, emit `{type:"halted", reason:"interrupted_by_user"}` then `{type:"complete"}`. Send failures MUST be swallowed (client may have disconnected).
50. **R50 — Re-raise**. After all of R45–R49, `_stream_response` MUST re-raise `CancelledError` so `handle_chat_connection.current_stream` completes with `CancelledError` (not a regular return).

### R-Error path

51. **R51 — No-API-key error**. If `ANTHROPIC_API_KEY` is unset, `_stream_response` MUST emit `{type:"error", error:"ANTHROPIC_API_KEY not configured on server"}` + `{type:"complete"}` and return BEFORE any Claude call and BEFORE loading history.
52. **R52 — Missing SDK**. If `import anthropic` raises `ImportError`, emit `{type:"error", error:"anthropic SDK not installed"}` + `{type:"complete"}` and return.
53. **R53 — API error surface**. `anthropic.APIError` MUST be caught and surfaced as `{type:"error", error:"Claude API error: <message>"}` + `{type:"complete"}`. **No `halted` event in this path** — that's reserved for interrupt.
54. **R54 — Generic exception surface**. Any other exception inside the try block MUST be caught and surfaced as `{type:"error", error:str(e)}` + `{type:"complete"}`.

### R-Generation job polling

55. **R55 — `tool_progress` is the only mid-tool WS emission**. `_await_generation_job` is the sole function that emits `tool_progress` frames in the chat pipeline. Regular DB tools emit no progress.
56. **R56 — Progress throttling**. A `tool_progress` frame MUST be emitted only when the `completed` counter changes vs. `last_completed`. No-change polls MUST NOT emit.
57. **R57 — Wall-clock timeout, job survives**. If 900s elapses without terminal status, return an error dict but MUST NOT cancel the underlying job (it keeps running). This preserves the "generation jobs survive disconnect" invariant at the polling layer.

---

## Migration Contract

The following observable pipeline behavior MUST be preserved through any refactor. If a refactor cannot preserve one of these, it is NOT a pure refactor and MUST come with a spec amendment.

- **Wire-level**: event frames emitted to the client, their ordering, and the shape of their bodies (covered by the contract spec; this spec does not re-spec them — it only names the requirements in this pipeline that produce them).
- **History window**: exactly 50 messages, per-user, oldest-first, JSON-decoded for assistant rows only.
- **Interrupt semantics**: partial text flushed to `all_blocks`; persisted to DB via `_add_message`; `message` echoed with `interrupted:true`; `halted` emitted; `complete` emitted; `CancelledError` re-raised. Persist failures logged, not propagated.
- **Elicitation timeout**: 300s → auto-decline; `CancelledError` re-raised.
- **Tool-loop cap**: 10 iterations per user message. (The contract spec also requires a `tool_loop_exceeded` event; that is R29 in the contract spec, orthogonal to this pipeline spec.)
- **Dispatcher precedence**: `_stream_response` bridge > `_execute_tool`; inside `_execute_tool`, plugin (`__` name) > built-in if-chain > unknown-tool error. Built-in tools cannot be shadowed by a bridge tool UNLESS the bridge claims the exact same name (undefined — OQ-2).
- **Destructive classifier**: allowlist > plugin flag > patterns.
- **Model/token**: `claude-sonnet-4-20250514` / 4096.
- **MCP bridge**: lazy connect via background task; degraded mode on failure; per-connection lifecycle.
- **Single-reader WS**: exactly one reader; elicitation routed through futures dict.

**Reference (layer above)**: `scenecraft/agent/specs/local.chat-tool-dispatch-and-elicitation.md` owns the **what** of each frame's shape + the **what** of precedence/timeout from the client's perspective. This spec owns the **how** of the engine-side internals producing those frames.

---

## Internal Units Inventory (refactor-safe targets)

The following internals are candidates for restructuring. Pure refactor is allowed iff the Migration Contract holds.

| # | Internal | Current Location | Refactor Candidate | Behavior-equivalent if |
|---|---|---|---|---|
| U1 | `_execute_tool` if/elif chain (~150 lines) | chat.py 4962–5149 | Dispatch table `BUILTIN_TOOLS: dict[str, Callable]` | Every current `name == "..."` branch resolves to the same `_exec_*` call with identical args; unknown name still produces `{"error": "unknown tool: ..."}` |
| U2 | `TOOLS` module-level list + 34 `*_TOOL` dicts | chat.py 1429–1464 (+ definitions above) | Separate module `chat/tools/catalog.py` or one file per tool | Merged output identical per R8–R11 |
| U3 | Destructive classifier | chat.py 1473–1530 | Extract to `chat/destructive.py` | Same return-value table for same inputs per R13–R15 |
| U4 | Connection read-loop switch | chat.py 5247–5294 | Extract to `chat/handlers.py` with per-type handlers | Same frame-type semantics per R37–R44 |
| U5 | `_stream_response` body | chat.py 5309–5582 | Split into `prepare_messages`, `run_tool_loop`, `persist_and_finalize`, `handle_interrupt` | Same observable sequence of WS frames and DB writes per R27–R50 |
| U6 | History decode/encode logic | chat.py 52–58, 80–87, 5504–5508 | Extract `serialize_assistant_content` / `decode_assistant_content` | Same round-trip: list-of-blocks ↔ JSON string; plain text unchanged |
| U7 | System prompt builder | chat.py 106–225 | Split into `PromptContext` dataclass + template | Same output string byte-for-byte for same DB state |
| U8 | `_await_generation_job` polling | chat.py 4872–4922 | Replace poll loop with job-manager subscription when ws_server gets a pub/sub API | Same `tool_progress` emissions on same completed-counter transitions; same terminal return shape |
| U9 | Elicitation waiters dict | chat.py 5231, 5278–5286, 1770–1796 | Wrap in `ElicitationBroker` class | Same futures-dict protocol; same 300s timeout; same CancelledError re-raise |
| U10 | MCP bridge kickoff | chat.py 5211–5219 | Move to `ChatSession.__aenter__` / DI | Same fire-and-forget + degraded-mode semantics |

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|---|---|---|
| 1 | New user message arrives | Halt in-flight stream, persist user row to `chat_messages`, echo `message` frame, spawn new stream task | `user-message-persists-and-spawns-stream` |
| 2 | Stream loads history | `_get_messages(..., limit=50)` called; result ordered oldest-first; assistant JSON-blocks decoded | `history-window-50-oldest-first`, `assistant-json-content-decoded`, `user-content-not-decoded` |
| 3 | System prompt built per stream | `_build_system_prompt` called once per `_stream_response`; DB counts re-read; default meta used on missing keys | `system-prompt-dynamic-per-call`, `system-prompt-default-meta`, `system-prompt-excludes-soft-deleted` |
| 4 | Tool catalog assembled | Three-key entries; built-ins → plugins → bridge order; materialized once | `catalog-three-key-shape`, `catalog-merge-order`, `catalog-single-materialization` |
| 5 | Bridge unavailable on first stream | `bridge.all_tools()` returns `[]`; stream proceeds with builtins + plugins | `bridge-empty-first-stream` |
| 6 | Bridge connects mid-session | New bridge tools appear on next `_stream_response` call, not the current one | `bridge-connect-appears-next-stream` |
| 7 | Claude streams text_delta | `chunk` frame emitted; `streamed_text_this_turn` appended | `text-delta-emits-chunk-and-buffers` |
| 8 | Claude starts tool_use block | `tool_call` frame emitted once per tool id | `tool-call-emitted-once-per-id` |
| 9 | Tool loop runs 10 iterations | Exactly 10 `messages.stream(...)` calls; then persist + `message` + `complete` | `ten-iteration-cap` |
| 10 | Claude returns `stop_reason != "tool_use"` | Loop exits early; persist + message + complete | `early-exit-on-non-tool-stop-reason` |
| 11 | Plugin-namespaced tool dispatched | `_execute_tool` routes through `PluginHost.get_mcp_tool`; context dict passed | `plugin-dispatch-with-context` |
| 12 | Plugin handler raises | `({"error":"ExcType: msg"}, True)` returned | `plugin-exception-wrapped` |
| 13 | Plugin handler returns non-dict | Error dict with `non-dict` message returned; `is_error=True` | `plugin-non-dict-error` |
| 14 | Unknown tool name | `{"error":"unknown tool: <name>"}` returned; `is_error=True` | `unknown-tool-error` |
| 15 | Bridge tool dispatched | `bridge.call_tool` called instead of `_execute_tool` | `bridge-dispatch-preferred-in-stream` |
| 16 | Destructive allowlist match | `_is_destructive` returns `False` despite pattern match | `allowlist-wins-over-pattern` |
| 17 | Plugin destructive flag | `_is_destructive` returns plugin's `destructive:bool` | `plugin-flag-returned` |
| 18 | PluginHost raises during classifier lookup | Falls through to pattern check; no exception propagates | `classifier-lookup-exception-swallowed` |
| 19 | Elicitation awaits 300s without response | Returns `"decline"`; waiter popped | `elicitation-timeout-decline` |
| 20 | Elicitation task cancelled mid-wait | `CancelledError` re-raised; waiter popped in finally | `elicitation-cancel-reraises` |
| 21 | User sends new message mid-stream | Cancel → flush partial → persist with `interrupted:true` → `halted` → `complete` → re-raise | `interrupt-flushes-and-persists`, `interrupt-emits-halted-and-complete` |
| 22 | Empty cancel (no text, no tools) | No persist; still emit `halted` + `complete`; re-raise | `empty-cancel-no-persist` |
| 23 | Persist fails during cancel | Error logged; `CancelledError` still propagates; `halted` + `complete` still attempted | `cancel-persist-failure-logged-not-raised` |
| 24 | `ANTHROPIC_API_KEY` missing | `error` + `complete` emitted; no Claude call; no history load | `no-api-key-early-return` |
| 25 | `anthropic` SDK missing | `error` + `complete` emitted; no history load | `no-sdk-early-return` |
| 26 | Claude API raises `APIError` | `error` (with msg) + `complete`; no `halted` | `api-error-no-halted` |
| 27 | Generic exception in stream | `error` + `complete`; no `halted` | `generic-error-no-halted` |
| 28 | WS closes mid-stream | Cancel in-flight stream; bridge closed; exceptions logged not propagated | `ws-close-cleans-up` |
| 29 | Invalid JSON frame | `{type:"error", error:"Invalid JSON"}` emitted; loop continues | `invalid-json-loop-continues` |
| 30 | Unknown frame type | Silently ignored; loop continues | `unknown-frame-type-ignored` |
| 31 | `message` with empty content | Silently dropped (no persist, no stream) | `empty-message-dropped` |
| 32 | `stop` frame | Halt stream; do not spawn new | `stop-halts-only` |
| 33 | `ping` frame | Reply `pong` | `ping-pong` |
| 34 | Stale `elicitation_response` (no waiter) | Dropped silently; loop continues | `stale-elicitation-dropped` |
| 35 | Two rapid `message` frames | First stream cancelled + persisted before second starts | `rapid-messages-serialize` |
| 36 | `_await_generation_job` progress unchanged | No `tool_progress` frame emitted | `progress-throttled-on-no-change` |
| 37 | `_await_generation_job` progress changes | `tool_progress` frame emitted with `pct = completed/total` | `progress-emitted-on-counter-change` |
| 38 | `_await_generation_job` times out (900s) | Error dict returned; underlying job NOT cancelled | `job-poll-timeout-does-not-cancel-job` |
| 39 | `_await_generation_job` `send` fails | Failure swallowed; polling continues | `progress-send-failure-swallowed` |
| 40 | Tool progress broadcast to WS clients not on chat session | `undefined` | → [OQ-1](#open-questions) |
| 41 | Plugin tool name collides with built-in (bypassing `__` invariant) | `undefined` | → [OQ-2](#open-questions) |
| 42 | History window filled with tool-use blocks | `undefined` — Claude may see fewer than 50 conversational turns | → [OQ-3](#open-questions) |
| 43 | Client disconnects while elicitation is awaiting | `undefined` — waiter may leak | → [OQ-4](#open-questions) |
| 44 | MCP bridge connect succeeds after initial failure (retry?) | `undefined` — currently no retry | → [OQ-5](#open-questions) |
| 45 | Anthropic SDK pinned version | `undefined` — not pinned in `pyproject.toml` | → [OQ-6](#open-questions) |

---

## Behavior (step-by-step)

### Connection lifecycle (`handle_chat_connection`)

1. Log `"Chat connected: project=... user=..."`.
2. Instantiate `MCPBridge()`.
3. `asyncio.create_task(_bg_connect_service("remember"))` — NEVER awaited.
4. Initialize `current_stream = None`, `elicitation_waiters = {}`.
5. Enter `async for raw in ws:` — the only WS reader for this connection.
6. For each frame:
   - `json.loads` → on `JSONDecodeError`, send `{type:"error", error:"Invalid JSON"}`, `continue`.
   - Branch on `data.get("type")`:
     - `"message"`: strip content; if empty, `continue`. Else `await _halt_current_stream()`, `_add_message(role="user", ...)`, send `message` echo, `current_stream = asyncio.create_task(_stream_response(...))`.
     - `"elicitation_response"`: `waiters.pop(id, None)` → if non-done future, `fut.set_result(action)`.
     - `"stop"`: `await _halt_current_stream()`.
     - `"ping"`: send `{type:"pong"}`.
     - Anything else: ignored.
7. On exception in loop: log, fall through to `finally`.
8. `finally`: `await _halt_current_stream()`, `await bridge.close()` (both try/except + log), log disconnect.

### Stream (`_stream_response`)

1. Check `ANTHROPIC_API_KEY` → missing → emit `error` + `complete`, return.
2. `import anthropic` → `ImportError` → emit `error` + `complete`, return.
3. `history = _get_messages(project_dir, user_id, limit=50)`.
4. `messages = _history_to_claude_messages(history)`.
5. `system_prompt = _build_system_prompt(project_dir, project_name)`.
6. `client = anthropic.AsyncAnthropic(api_key=api_key)`.
7. Merge `tools_for_claude = list(TOOLS) + plugin_contributed + mcp_tools`.
8. Init `all_blocks=[]`, `tool_calls_log=[]`, `announced_tool_ids=set()`, `streamed_text_this_turn=""`.
9. Enter `try:`.
10. `for _ in range(10):`
    - `streamed_text_this_turn = ""`.
    - `async with client.messages.stream(...)` with the hardcoded model + max_tokens.
    - Inside: iterate events.
      - `content_block_start` + `tool_use`: if `tid` new, add to `announced_tool_ids`, send `tool_call` with `input={}`.
      - `content_block_delta` + `text_delta`: append to buffer, send `chunk`.
    - `final = await stream.get_final_message()`; clear buffer.
    - Accumulate `final.content` blocks into `all_blocks`; collect `turn_tool_uses`.
    - If `final.stop_reason != "tool_use"` or no tool uses: `break`.
    - For each `tu`:
      - If destructive (`_is_destructive`): emit `elicitation` frame; `await _recv_elicitation_response(...)`; if not `"accept"`, emit cancel `tool_result`, append cancel block + log row, `continue`.
      - Else: dispatch via `bridge.call_tool` (if `bridge.has_tool`) else `_execute_tool`. Measure `dt_ms`. Emit `tool_result` with the result. Append result block + log row.
    - Append `{role:"assistant", content:[...]}` and `{role:"user", content:tool_result_blocks}` to `messages`.
11. After loop: persist assistant row (JSON blocks if any non-text block, else concatenated text). Send `message` with persisted content. Send `complete`.
12. `except asyncio.CancelledError:` — interrupt path (see Interrupt below). Re-raise.
13. `except anthropic.APIError:` — send `error` + `complete`.
14. `except Exception:` — send `error` + `complete`.

### Interrupt path (`_stream_response` CancelledError handler)

1. If `streamed_text_this_turn`: append `{"type":"text", "text": ...}` to `all_blocks`.
2. If `all_blocks or tool_calls_log`:
   - Compute `has_non_text`, `persisted_content` (JSON of blocks OR concatenated text).
   - `try`: `_add_message(...)` with `tool_calls=tool_calls_log or None`.
   - Decorate the returned dict: `content` = `all_blocks` (if non-text), `tool_calls` if any, `interrupted=True`.
   - `try`: send `message` frame with the decorated dict.
   - Any exception here: log, swallow.
3. `try`: send `{type:"halted", reason:"interrupted_by_user"}` — swallow send errors.
4. `try`: send `{type:"complete"}` — swallow send errors.
5. `raise` (re-raises the `CancelledError` so the connection handler sees it).

### Destructive classifier (`_is_destructive`)

1. `name = tool_name.lower()`.
2. If `name in _DESTRUCTIVE_TOOL_ALLOWLIST`: return `False`.
3. If `"__" in name`:
   - Try `PluginHost.get_mcp_tool(tool_name)` — swallow any exception.
   - If returned tool is non-None: return `bool(tool.destructive)`.
4. Return `True` iff any `p in _DESTRUCTIVE_TOOL_PATTERNS` appears as substring in `name`.

### Elicitation waiter (`_recv_elicitation_response`)

1. `fut = loop.create_future()`; `waiters[elicitation_id] = fut`.
2. `try`: `action = await asyncio.wait_for(fut, timeout=300)`.
3. `except asyncio.TimeoutError`: log; `return "decline"`.
4. `except asyncio.CancelledError`: `raise` (re-raise).
5. `finally`: `waiters.pop(elicitation_id, None)`.
6. Return `"accept" if action == "accept" else "decline"`.

---

## Acceptance Criteria

- [ ] `chat_messages` schema preserved; `_add_message` returns the documented dict.
- [ ] `_get_messages(limit=50)` is used by every `_stream_response` call.
- [ ] `_build_system_prompt` is called fresh per stream; DB counts respect soft-delete.
- [ ] Tool catalog has three-key entries in `builtins + plugins + bridge` order and is materialized once per stream.
- [ ] Destructive classifier follows allowlist > plugin flag > patterns.
- [ ] `_execute_tool` dispatches plugin-namespaced tools before built-ins; unknown tools return the documented error dict.
- [ ] `_stream_response` uses bridge before `_execute_tool`.
- [ ] Elicitation uses the `elicitation_waiters` futures-dict; 300s timeout → decline; `CancelledError` re-raised.
- [ ] 10-iteration cap observed; `tool_call` de-duped by `announced_tool_ids`.
- [ ] Interrupt flushes `streamed_text_this_turn`, persists (tolerating failure), emits `message`+`halted`+`complete`, re-raises.
- [ ] `ANTHROPIC_API_KEY` missing returns cleanly with no Claude call; `APIError` ≠ `halted`.
- [ ] MCP bridge connect is fire-and-forget; `bridge.close()` called in `finally` with errors swallowed.
- [ ] `claude-sonnet-4-20250514` + `max_tokens=4096` preserved.
- [ ] `_await_generation_job` emits `tool_progress` only on `completed` counter change; timeout returns error without cancelling the job.
- [ ] Every behavior-table row with a named test has a matching `#### Test:`.
- [ ] Every `undefined` row has a matching Open Question.

---

## Tests

### Base Cases

#### Test: user-message-persists-and-spawns-stream (covers R40, R41)

**Given**: An open chat WS; no `current_stream`.
**When**: Client sends `{type:"message", content:"hi"}`.
**Then**:
- **halt-called**: `_halt_current_stream` was awaited (no-op).
- **user-row-persisted**: a `chat_messages` row with `role="user"`, `content="hi"` exists.
- **echo-sent**: a `message` frame was sent back.
- **stream-spawned**: `current_stream` is a non-done `asyncio.Task`.

#### Test: history-window-50-oldest-first (covers R3, R4)

**Given**: DB has 120 rows for `user_id="local"`.
**When**: `_stream_response` runs.
**Then**:
- **limit-50**: the SQL `LIMIT` is `50`.
- **oldest-first**: `messages` passed to Claude starts with the oldest of the 50 rows.

#### Test: assistant-json-content-decoded (covers R3)

**Given**: A `chat_messages` row with `role="assistant"`, `content='[{"type":"text","text":"hi"}]'`.
**When**: `_get_messages` runs.
**Then**:
- **content-is-list**: the returned dict's `content` is `[{"type":"text","text":"hi"}]`.

#### Test: user-content-not-decoded (covers R3)

**Given**: A `chat_messages` row with `role="user"`, `content='["not decoded"]'`.
**When**: `_get_messages` runs.
**Then**:
- **content-is-string**: the returned `content` is the literal string `'["not decoded"]'`.

#### Test: system-prompt-dynamic-per-call (covers R5)

**Given**: `_build_system_prompt` is called, then a keyframe is inserted, then `_build_system_prompt` is called again.
**When**: Both outputs are compared.
**Then**:
- **second-count-higher**: the keyframe count in the second output is exactly one higher than the first.

#### Test: system-prompt-default-meta (covers R6)

**Given**: `meta` table is empty.
**When**: `_build_system_prompt(project_dir, "proj")` runs.
**Then**:
- **fps-default**: `"FPS: 24"` appears in the output.
- **resolution-default**: `"1920,1080"` appears.
- **title-default**: `"proj"` appears as the title.

#### Test: system-prompt-excludes-soft-deleted (covers R7)

**Given**: 3 keyframes, one with `deleted_at` set.
**When**: `_build_system_prompt` runs.
**Then**:
- **kf-count-2**: `"Keyframes: 2"` appears.

#### Test: catalog-three-key-shape (covers R8)

**Given**: Any `_stream_response` call.
**When**: `tools_for_claude` is captured.
**Then**:
- **all-entries-three-keys**: every entry has exactly `{name, description, input_schema}`.

#### Test: catalog-merge-order (covers R9, R11, R12)

**Given**: 34 built-ins, 2 plugin tools, 1 bridge tool.
**When**: `tools_for_claude` is built.
**Then**:
- **order**: first 34 entries are the `TOOLS` list, next 2 are the plugin tools (using `t.full_name`), last 1 is the bridge tool.

#### Test: catalog-single-materialization (covers R10)

**Given**: `_stream_response` entering a 3-iteration tool loop.
**When**: The merge is instrumented.
**Then**:
- **build-count-one**: `list(TOOLS) + plugin_contributed + mcp_tools` is constructed exactly once per `_stream_response` call.

#### Test: bridge-empty-first-stream (covers R34)

**Given**: Bridge background connect has not completed.
**When**: `_stream_response` runs.
**Then**:
- **bridge-all-tools-empty**: `bridge.all_tools()` returns `[]`.
- **no-error**: stream proceeds normally.

#### Test: bridge-connect-appears-next-stream (covers R35)

**Given**: Stream 1 runs with `bridge.all_tools() == []`; then bridge background connect completes and exposes `remember_*` tools; stream 2 starts.
**When**: Stream 2 merges the catalog.
**Then**:
- **new-tools-included**: Stream 2's `tools_for_claude` contains the `remember_*` entries; stream 1's did not.

#### Test: text-delta-emits-chunk-and-buffers (covers R29, R30)

**Given**: Claude emits a `text_delta` with `text="hello"`.
**When**: The loop handles it.
**Then**:
- **chunk-sent**: `{type:"chunk", content:"hello"}` sent.
- **buffer-appended**: `streamed_text_this_turn == "hello"`.

#### Test: tool-call-emitted-once-per-id (covers R28)

**Given**: Claude emits `content_block_start` for the same tool_use id twice (malformed).
**When**: Both are handled.
**Then**:
- **one-frame**: exactly one `tool_call` frame emitted for that id.

#### Test: ten-iteration-cap (covers R27)

**Given**: Claude keeps returning `stop_reason:"tool_use"` with one tool_use per turn.
**When**: `_stream_response` runs.
**Then**:
- **ten-streams**: `client.messages.stream(...)` called exactly 10 times.
- **persisted**: one assistant row persisted.
- **complete-emitted**: `complete` frame sent.

#### Test: early-exit-on-non-tool-stop-reason (covers R27)

**Given**: First iteration returns `stop_reason:"end_turn"`, no tool uses.
**When**: Loop completes iteration 1.
**Then**:
- **no-second-stream**: `client.messages.stream` called exactly once.
- **persisted**: assistant row persisted.

#### Test: plugin-dispatch-with-context (covers R16, R20, R21)

**Given**: Plugin tool `foo__bar` registered with handler `h`.
**When**: `_execute_tool(project_dir, "foo__bar", None, ws=ws, tool_use_id="t", project_name="p")`.
**Then**:
- **input-coerced**: `h` received `{}` as first arg (not `None`).
- **context-shape**: `h` received `{"project_dir":..., "project_name":"p", "ws":ws, "tool_use_id":"t"}` — exactly those four keys.

#### Test: plugin-exception-wrapped (covers R19)

**Given**: Plugin handler raises `RuntimeError("boom")`.
**When**: `_execute_tool` dispatches.
**Then**:
- **error-dict**: returns `({"error":"RuntimeError: boom"}, True)`.

#### Test: plugin-non-dict-error (covers R19)

**Given**: Plugin handler returns the string `"ok"`.
**When**: `_execute_tool` dispatches.
**Then**:
- **error-dict**: returns an error dict mentioning `non-dict`; `is_error=True`.

#### Test: unknown-tool-error (covers R18)

**Given**: `_execute_tool` called with `name="nonexistent"` (no plugin match, no built-in).
**When**: Dispatch runs.
**Then**:
- **error-dict**: returns `({"error":"unknown tool: nonexistent"}, True)`.

#### Test: bridge-dispatch-preferred-in-stream (covers R17)

**Given**: `bridge.has_tool("foo") == True` and `_execute_tool` is also capable of handling `"foo"`.
**When**: A `tool_use` for `"foo"` arrives.
**Then**:
- **bridge-called**: `bridge.call_tool("foo", ...)` invoked.
- **execute-tool-not-called**: `_execute_tool` NOT invoked for this tool_use.

#### Test: allowlist-wins-over-pattern (covers R13)

**Given**: Tool name `generate_dsp` (matches `"generate_"` pattern).
**When**: `_is_destructive` runs.
**Then**:
- **returns-false**: `False`.

#### Test: plugin-flag-returned (covers R14)

**Given**: Plugin tool `foo__delete_thing` registered with `destructive=False`.
**When**: `_is_destructive("foo__delete_thing")` runs.
**Then**:
- **returns-false**: `False` (plugin flag wins over the `"delete"` substring).

#### Test: classifier-lookup-exception-swallowed (covers R14)

**Given**: `PluginHost.get_mcp_tool` raises unexpectedly for name `foo__bar`.
**When**: `_is_destructive("foo__bar")` runs.
**Then**:
- **no-exception-propagates**: the classifier returns a bool (from pattern match), NOT raising.

#### Test: elicitation-timeout-decline (covers R23)

**Given**: A registered waiter for `elic_X`.
**When**: No response arrives for 300s.
**Then**:
- **returns-decline**: `_recv_elicitation_response` returns `"decline"`.
- **waiter-popped**: `elicitation_waiters` no longer contains `elic_X`.

#### Test: elicitation-cancel-reraises (covers R24, R25)

**Given**: A registered waiter; surrounding task is cancelled.
**When**: `CancelledError` fires.
**Then**:
- **reraised**: `CancelledError` propagates out of `_recv_elicitation_response`.
- **waiter-popped**: `finally` removed the entry.

#### Test: interrupt-flushes-and-persists (covers R45, R46, R48)

**Given**: Stream has emitted `chunk` frames totalling `"partial "` and is awaiting elicitation.
**When**: The task is cancelled (user sent a new message).
**Then**:
- **buffer-flushed**: a `{"type":"text","text":"partial "}` block appears in the persisted `all_blocks`.
- **row-written**: one assistant `chat_messages` row with that content.
- **message-interrupted**: the emitted `message` frame has `message.interrupted == True`.

#### Test: interrupt-emits-halted-and-complete (covers R49, R50)

**Given**: Same setup as above.
**When**: Cancel runs.
**Then**:
- **halted-sent**: `{type:"halted", reason:"interrupted_by_user"}` sent.
- **complete-sent**: `{type:"complete"}` sent after `halted`.
- **cancelled-error-propagates**: `CancelledError` raised out of `_stream_response`.

#### Test: no-api-key-early-return (covers R51)

**Given**: `ANTHROPIC_API_KEY` not in env.
**When**: `_stream_response` invoked.
**Then**:
- **error-frame**: `{type:"error", error:"ANTHROPIC_API_KEY not configured on server"}` sent.
- **complete-frame**: `complete` sent after.
- **no-claude-call**: `anthropic.AsyncAnthropic` never constructed; `messages.stream` never called.
- **no-history-load**: `_get_messages` not called.

#### Test: api-error-no-halted (covers R53)

**Given**: `anthropic.APIError` raised mid-stream.
**When**: Caught.
**Then**:
- **error-frame**: `error` frame with `"Claude API error: ..."` sent.
- **complete-frame**: `complete` sent.
- **no-halted**: NO `halted` frame sent.

#### Test: ws-close-cleans-up (covers R36, R44)

**Given**: In-flight `_stream_response` and open bridge.
**When**: WS connection closes.
**Then**:
- **stream-halted**: `current_stream` was cancelled and awaited.
- **bridge-closed**: `bridge.close()` was called.
- **no-exception-propagated**: `handle_chat_connection` returned without raising.

#### Test: invalid-json-loop-continues (covers R39)

**Given**: Client sends non-JSON text.
**When**: Read loop parses.
**Then**:
- **error-frame**: `{type:"error", error:"Invalid JSON"}` sent.
- **next-frame-processed**: subsequent valid frame still handled.

#### Test: ping-pong (covers R43)

**Given**: Client sends `{type:"ping"}`.
**When**: Read loop handles.
**Then**:
- **pong-sent**: `{type:"pong"}` sent.

#### Test: stop-halts-only (covers R42)

**Given**: In-flight stream.
**When**: Client sends `{type:"stop"}`.
**Then**:
- **stream-cancelled**: the stream task is cancelled.
- **no-new-stream**: no new `current_stream` spawned.

### Edge Cases

#### Test: assistant-json-content-not-a-list-stays-string

**Given**: Assistant row with `content='"just a quoted string"'` (valid JSON but not a list).
**When**: `_get_messages` runs.
**Then**:
- **content-unchanged**: `content` remains the literal string `'"just a quoted string"'`.

#### Test: empty-message-dropped (covers R41)

**Given**: Client sends `{type:"message", content:"   "}`.
**When**: Read loop handles.
**Then**:
- **no-persist**: no `chat_messages` row inserted.
- **no-stream-spawned**: `current_stream` unchanged.

#### Test: unknown-frame-type-ignored (covers R38)

**Given**: Client sends `{type:"nonsense"}`.
**When**: Read loop handles.
**Then**:
- **no-error-frame**: no `error` frame sent.
- **loop-continues**: next frame still processed.

#### Test: stale-elicitation-dropped

**Given**: Client sends `{type:"elicitation_response", id:"nonexistent", action:"accept"}`.
**When**: Read loop handles.
**Then**:
- **no-error-frame**: no error emitted.
- **no-state-change**: `elicitation_waiters` unchanged.

#### Test: rapid-messages-serialize (covers R40, R44)

**Given**: Two `message` frames arrive in quick succession.
**When**: Both processed.
**Then**:
- **first-cancelled-and-awaited**: the first stream task reached `CancelledError` and its partial was persisted BEFORE the second stream started.
- **exactly-one-running**: at any instant `current_stream` points to at most one non-done task.

#### Test: empty-cancel-no-persist (covers R46)

**Given**: `_stream_response` cancelled before any text or tool output.
**When**: Cancel handler runs.
**Then**:
- **no-row-written**: no assistant `chat_messages` row created.
- **halted-sent**: `halted` frame still sent.
- **cancelled-error-propagates**: `CancelledError` raised.

#### Test: cancel-persist-failure-logged-not-raised (covers R47)

**Given**: `_add_message` raises during the cancel path.
**When**: Cancel handler runs.
**Then**:
- **error-logged**: a `"Failed to persist partial assistant message"` log line emitted.
- **cancelled-error-propagates**: `CancelledError` still raised.
- **halted-still-attempted**: `halted` send attempted (swallowed on failure).

#### Test: no-sdk-early-return (covers R52)

**Given**: `import anthropic` raises `ImportError`.
**When**: `_stream_response` invoked.
**Then**:
- **error-frame**: `{type:"error", error:"anthropic SDK not installed"}` sent.
- **complete-frame**: `complete` sent after.

#### Test: generic-error-no-halted (covers R54)

**Given**: A `RuntimeError` raised inside the stream try block (not `APIError`, not `CancelledError`).
**When**: Caught.
**Then**:
- **error-frame**: `{type:"error", error:"<str(e)>"}` sent.
- **complete-frame**: `complete` sent.
- **no-halted**: NO `halted` frame.

#### Test: progress-throttled-on-no-change (covers R56)

**Given**: `_await_generation_job` polling; job's `completed` counter stays at 3.
**When**: Five poll cycles elapse.
**Then**:
- **one-frame**: exactly one `tool_progress` frame emitted (for the initial transition to 3).

#### Test: progress-emitted-on-counter-change (covers R56)

**Given**: Job's `completed` transitions 0 → 1 → 2; `total=4`.
**When**: Poll cycles elapse across the transitions.
**Then**:
- **three-frames**: three `tool_progress` frames emitted.
- **pcts**: `pct` values are `0.0`, `0.25`, `0.5` respectively.

#### Test: job-poll-timeout-does-not-cancel-job (covers R57)

**Given**: A job never reaches terminal state; 900s elapses in the poll.
**When**: `_await_generation_job` returns.
**Then**:
- **error-result**: `({"error":"generation job ... did not finish within 900s; it may still be running"}, True)`.
- **job-still-running**: `job_manager.get_job(job_id).status` is still not `"completed"` or `"failed"` (no cancel was issued).

#### Test: progress-send-failure-swallowed (covers R55)

**Given**: `ws.send` raises during a `tool_progress` emission.
**When**: Polling continues.
**Then**:
- **no-exception**: `_await_generation_job` does not raise.
- **next-poll-attempted**: the loop sleeps and polls again.

#### Test: multiple-tool-uses-per-turn-order-preserved

**Given**: Claude emits `[tool_use_A, tool_use_B]` in one turn; both non-destructive.
**When**: Both execute.
**Then**:
- **emit-order**: `tool_result` for A emitted before `tool_result` for B.
- **log-order**: `tool_calls_log` has A before B.

#### Test: bridge-close-error-swallowed (covers R36)

**Given**: `bridge.close()` raises inside the connection-handler `finally`.
**When**: WS disconnect runs.
**Then**:
- **error-logged**: `"bridge.close raised: ..."` log.
- **no-exception-propagated**: `handle_chat_connection` returns cleanly.

#### Negative: no-concurrent-ws-recv (covers R22, R37)

**Given**: The chat connection code paths.
**When**: Statically inspecting `_stream_response`.
**Then**:
- **no-ws-recv**: `_stream_response` contains no call to `ws.recv()` — elicitation responses arrive only via `elicitation_waiters`.

#### Negative: no-catalog-rebuild-per-iter (covers R10)

**Given**: An instrumented `_stream_response` running a 5-iter tool loop.
**When**: The merge call site is counted.
**Then**:
- **merge-called-once**: the merge (`list(TOOLS) + plugin_contributed + mcp_tools`) ran exactly once for the whole `_stream_response` call.

#### Negative: no-model-upgrade-silently (covers R32)

**Given**: The `messages.stream(...)` kwargs.
**When**: Inspected.
**Then**:
- **model-literal**: `model="claude-sonnet-4-20250514"` is the literal value.
- **max-tokens-literal**: `max_tokens=4096` is the literal value.

---

## Non-Goals

- **Re-specifying WS frame shapes / names.** Owned by the contract spec. This spec references frames by the name their emission produces.
- **`_history_to_claude_messages` block-splitting rules.** Owned by the contract spec (R3 there).
- **Per-tool handler semantics.** Each `_exec_*` is a feature-spec concern.
- **Frontend behavior.** Out of scope.
- **Job manager internals.** Only `_await_generation_job`'s polling + emission contract is in scope.
- **OAuth token store.** Treated as an opaque provider under `MCPBridge.connect`.
- **Retry policies for `MCPBridge.connect`.** There are none today; see OQ-5.
- **Concurrent chat connections for the same `user_id`.** Each connection has its own `bridge` and its own `elicitation_waiters`; cross-connection coordination is not in scope.
- **Migration of the `chat_messages` schema.** If the refactor needs to change it, that is a separate migration spec.
- **Replacement of the SQLite backend.** Same as above.

---

## Open Questions

**OQ-1 — `tool_progress` broadcast to off-session WS clients** (audit leak #19)
Today `_await_generation_job` emits `tool_progress` only on the chat-session WS. If a separate WS client is listening to `/ws/jobs`, does it see a parallel `core__job__*` frame? The engine's `JobManager` publishes its own events on the job bus independently. Whether the frontend de-dupes or double-renders is the frontend's concern; the engine-side question is whether there is accidental cross-talk into the chat-session WS. **Current state**: no known cross-talk in `chat.py` (`_await_generation_job` only sends to the `ws` arg). Flag retained as undefined until the refactor confirms the boundary under the new structure. **Proposed default**: preserve current isolation — chat WS receives `tool_progress` only; job bus receives `core__job__*` only.

**OQ-2 — Plugin tool name collision with a built-in** (audit leak #20)
Plugin tools are required by invariant to contain `"__"`. Built-ins never do. If a plugin manifest bypasses the invariant and registers `sql_query`, the current code silently has the plugin WIN (because `_execute_tool` checks `"__" in name` first, but for a plain `sql_query` there is no `"__"` — so the built-in wins anyway). However, the contract spec R30 codifies plugin > built-in. The engine-side reality today: because the bypass requires omitting `__`, the plugin's path inside `_execute_tool` is NOT taken — built-in wins silently. Refactor decision needed: align with the contract spec (plugin wins with WARNING log) or leave the de-facto engine behavior. **Proposed default**: align with contract spec; add a WARNING log in the catalog-merge step.

**OQ-3 — History window filled with tool-use blocks**
The 50-row window is a row count, not a conversational-turn count. A single assistant turn with 10 `tool_use` blocks expands into ~20 Claude messages after `_history_to_claude_messages` split. A window of 50 rows can therefore expose fewer than 50 conversational turns to Claude. Not an engine bug per se, but a surprise for callers expecting 50 turns of context. **Proposed default**: document as intended; callers who need more context should raise `limit` or pre-summarize.

**OQ-4 — `elicitation_waiters` leak on client disconnect mid-wait**
When the WS closes while a stream is awaiting an elicitation, the stream task is cancelled via `_halt_current_stream`, which triggers `_recv_elicitation_response`'s `CancelledError` path. The `finally` pops the waiter — so there is no leak in the normal close path. BUT: if the future has already been resolved (e.g. via a stale response that crossed paths with a cancel) and is sitting un-popped due to a bug, the dict grows. **Proposed default**: add a test for the exact cancel-mid-wait race; confirm waiter popped; flag undefined if a real leak is found. Currently believed leak-free.

**OQ-5 — MCP bridge connect succeeds after initial failure**
`_bg_connect_service` runs once per WS connection. If the initial attempt fails (no token, network blip), there is NO retry — the bridge stays empty for the life of the connection. Whether the user should be offered a "reconnect Remember" button or whether the engine should retry on a timer is unresolved. **Proposed default**: leave single-shot today; document explicitly; revisit if user feedback demands.

**OQ-6 — Anthropic SDK not pinned**
`pyproject.toml` does not pin the `anthropic` version. The streaming contract (`content_block_start`, `content_block_delta`, `get_final_message`, `stop_reason`) depends on the SDK's event shapes, which have changed across major versions. A refactor that codifies the streaming loop against a specific SDK version is safer. **Proposed default**: pin `anthropic>=0.39,<1.0` (or the current installed major) in `pyproject.toml` as part of the refactor.

---

## Related Artifacts

- **Contract spec (layer above)**: `../scenecraft/agent/specs/local.chat-tool-dispatch-and-elicitation.md`
- **Source files**:
  - `src/scenecraft/chat.py` (5,594 LOC)
  - `src/scenecraft/mcp_bridge.py` (214 LOC)
  - `src/scenecraft/chat_generation.py` (322 LOC; referenced via `_await_generation_job`)
- **Audit**: audit-2 §1B (chat pipeline), §3 leaks #19, #20, #21
- **Related planned specs**:
  - `local.job-manager-and-ws-events` — owns `/ws/jobs` + job lifecycle (consumer of `_await_generation_job`)
  - `local.plugin-host-and-manifest` — owns `PluginHost.list_mcp_tools` + `PluginHost.get_mcp_tool`
  - `local.plugin-api-surface-and-r9a` — owns the handler `context` shape

---

**Namespace**: local
**Spec**: engine-chat-pipeline
**Version**: 1.0.0
**Created**: 2026-04-27
**Status**: Active (retroactive; refactor-regression)
