# Spec: engine-mcp-bridge

> **Agent Directive**: This is an implementation-ready specification for the
> `MCPBridge` class and its chat-handler integration. Treat each requirement
> and test as the contract the system must satisfy. Do not guess behavior
> that appears in Open Questions — escalate instead.

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft

---

## Purpose

Define the observable behavior of the engine's **MCP bridge** — the component
that lets a chat connection pull tools from OAuth-authenticated external MCP
servers (initially Remember) over an SSE transport, merge those tools into
Claude's tool list, and route Claude tool calls back to the correct upstream
session.

---

## Source

- Mode: `--from-draft` (chat-supplied source description + code inspection)
- Primary source file: `src/scenecraft/mcp_bridge.py`
- Secondary integration source: `src/scenecraft/chat.py` — `handle_chat_connection` + `_bg_connect_service`
- Referenced audit: `agent/reports/audit-2-architectural-deep-dive.md` §1B, unit 8

---

## Scope

### In scope
- `MCPBridge` class: construction, connect, close, `all_tools`, `has_tool`, `call_tool`
- Lazy OAuth-backed connect per service (currently only `remember`)
- SSE transport with `Authorization: Bearer <token>` header
- Handshake / init / `list_tools` timeouts (10s SSE + ClientSession enter, 15s initialize + list_tools)
- `ClientSession` lifetime tied to an `AsyncExitStack` per service
- Tool discovery via `session.list_tools()` on connect
- Tool name prefixing rule: names for `service == "remember"` pass through unchanged; for any other service, names get a `<service>_` prefix unless they already start with one
- The `_tool_routing` dict (`tool_name → service`) and its role in `call_tool`
- `call_tool` 60s per-call timeout and content-flattening contract
- Degraded-mode fallback: on any connect failure, `all_tools()` returns the empty list and chat proceeds with built-ins only
- Fire-and-forget `_bg_connect_service("remember")` in `handle_chat_connection` — chat ws loop never blocks on MCP availability

### Out of scope
- OAuth token storage internals (`oauth_client.py` — token persistence, refresh plumbing)
- Remember MCP server-side behavior or its specific tool catalog
- Claude's tool-use loop itself (how the tools list is consumed downstream)
- Chat dispatcher's plugin-vs-MCP collision policy (only referenced; see OQ-3)
- Multi-user / multi-tenant concerns beyond a single `user_id` parameter
- Metrics / telemetry emission

---

## Requirements

1. **R1** — `MCPBridge()` constructs with no I/O and no sessions (`all_tools()` returns `[]`, `has_tool(x)` returns `False` for any `x`).
2. **R2** — `await bridge.connect(service, user_id)` returns `True` when the SSE handshake, `ClientSession` init, and `list_tools` all succeed, and registers the service's tools.
3. **R3** — `connect` returns `False` (never raises) when: service is unknown to `SERVICES`, no valid OAuth access token is available, the `mcp` SDK is not installed, the SSE handshake fails/times out, init fails/times out, or `list_tools` fails/times out.
4. **R4** — SSE handshake + `ClientSession.__aenter__` is bounded by 10s each; `session.initialize()` and `session.list_tools()` are each bounded by 15s.
5. **R5** — All SSE auth is via an `Authorization: Bearer <access_token>` header taken from `get_valid_access_token(user_id, service)`.
6. **R6** — Tool-name routing rule: if `service == "remember"` OR `tool.name` already starts with `"<service>_"`, the Claude-visible name is `tool.name` unchanged; otherwise it is `f"{service}_{tool.name}"`.
7. **R7** — For each discovered tool, `_tool_routing[claude_name] = service` is recorded and a Claude tool dict `{name, description, input_schema}` is appended to that session's `tools`, with `description` defaulting to `""` and `input_schema` defaulting to `{"type": "object", "properties": {}}` when the upstream tool omits them.
8. **R8** — `connect(service, user_id)` is idempotent per `MCPBridge` instance: if a session for `service` already exists, it returns `True` without reconnecting.
9. **R9** — `all_tools()` returns the concatenation of every live session's Claude tool dicts, in service-insertion order, and returns `[]` when no sessions are live.
10. **R10** — `has_tool(name)` returns `True` iff `name` is currently a key in `_tool_routing`.
11. **R11** — `call_tool(name, arguments)` routes to the session registered in `_tool_routing[name]`, strips the `<service>_` prefix from `name` for the upstream call when (and only when) `service != "remember"` AND `name.startswith(f"{service}_")`, and passes `arguments or {}` through.
12. **R12** — `call_tool` is bounded by a 60s timeout per call; on timeout it returns `({"error": "MCP tool <name> timed out"}, True)` and does not raise.
13. **R13** — `call_tool` returns `({"error": ...}, True)` (without raising) when: `name` is unknown to routing, the routed service has no live session, or the upstream `call_tool` raises any exception.
14. **R14** — `call_tool` flattens upstream `result.content`: each item's `.text` is used if present, otherwise the item is converted via `_item_to_dict` (preferring `model_dump()`, then `vars()`, then `{"value": str(item)}`). If the flattened list has exactly one string entry, it is unwrapped to a bare string.
15. **R15** — `call_tool` returns `({"output": <flat>}, False)` on success (upstream `isError` falsy) and `({"error": <flat>}, True)` when the upstream result has `isError` truthy.
16. **R16** — `close()` aclose()s every per-session `AsyncExitStack`, swallows exceptions from each, then clears `_sessions` and `_tool_routing`. It never raises.
17. **R17** — On failed `connect`, the partially-entered `AsyncExitStack` is `aclose()`d (errors swallowed), no `MCPSession` is registered, and no entries are added to `_tool_routing`.
18. **R18** — `handle_chat_connection` creates the `MCPBridge` synchronously and schedules `_bg_connect_service("remember")` via `asyncio.create_task`, then proceeds into the ws receive loop without awaiting connect.
19. **R19** — Until the background connect finishes, `bridge.all_tools()` returns `[]`; subsequent `_stream_response` invocations pick up newly-discovered tools on their next read of `all_tools()`.
20. **R20** — Any exception raised inside `_bg_connect_service` (including unexpected ones that `connect` would normally convert to `False`) is caught and logged; the chat loop continues.

### Target-State Requirements (per INV-8)

21. **R21 (target — OAuth refresh on 401)** — When an upstream MCP `call_tool` returns a 401 (or the SSE session raises an auth error), the bridge MUST: (a) invoke `get_valid_access_token(user_id, service, force_refresh=True)` to fetch a new token via OAuth refresh flow, (b) tear down the current `AsyncExitStack` + re-enter with the new token, (c) retry the call once. On refresh failure or a second 401, the bridge MUST enter degraded mode for that service — tools hidden from `all_tools()`, existing routing entries cleared.
22. **R22 (target — skip malformed tool schemas)** — During `list_tools()` processing, if a tool's `name` is not a non-empty string OR `inputSchema` is not `None` and not a JSON-object dict, the bridge MUST skip that tool, log a warning with a snippet of the malformed schema, and continue with the remaining valid tools. The connect itself MUST still return `True` so long as at least one valid tool or no tools at all are returned.
23. **R23 (target — universal `{service}__` namespace)** — All MCP tool names exposed to Claude MUST be `{service}__{tool_name}` (double-underscore separator). The `remember` service retains its current unprefixed names as a **transitional** exception for one release cycle; after that, `remember` tools also adopt the prefix. New services MUST use the prefix from day one.
24. **R24 (target — 300s `call_tool` timeout)** — Default `call_tool` timeout MUST be raised from 60s to 300s to accommodate legitimate long-running Remember queries. The timeout remains a single global default; per-tool overrides are out of scope.
25. **R25 (target — backoff retry for initial connect)** — If `_bg_connect_service` initial `connect(...)` returns `False`, the bridge MUST retry with exponential backoff (starting 5s, doubling to a 60s cap) for up to 5 minutes total wall-clock. On eventual success, the bridge MUST emit a `core__chat__mcp_tools_ready` event on the originating chat session's WS (per INV-4) so the client can refresh its tool surface.
26. **R26 (target — `asyncio.Lock` per service)** — `connect(service, user_id)` MUST acquire a per-service `asyncio.Lock` before the early-return check against `self._sessions`. Concurrent callers for the same service MUST serialize; the second caller awaits the first and then observes the `service in self._sessions` short-circuit, returning `True` without re-entering SSE.
27. **R27 (target — `core__chat__mcp_tools_ready` event on late-connect)** — Per INV-4, when `_bg_connect_service` succeeds after an initial failure (R25 retry path) OR when the first-attempt connect completes AFTER the WS loop has already processed user messages, the bridge MUST emit `core__chat__mcp_tools_ready` with payload `{service, tool_count}` scoped to the originating chat session. Client surfaces this as a subtle toast.

---

## Interfaces / Data Shapes

### Class

```python
class MCPBridge:
    def __init__(self) -> None: ...
    async def connect(self, service: str, user_id: str) -> bool: ...
    async def close(self) -> None: ...
    def all_tools(self) -> list[dict]: ...
    def has_tool(self, name: str) -> bool: ...
    async def call_tool(self, name: str, arguments: dict) -> tuple[dict, bool]: ...
```

### `MCPSession` (internal)

```python
@dataclass
class MCPSession:
    service: str
    session: Any                # mcp.ClientSession
    tools: list[dict]           # Claude-shaped tool descriptors
    exit_stack: AsyncExitStack | None
```

### Claude tool descriptor (shape emitted by `all_tools()`)

```json
{
  "name": "remember_search_memory",
  "description": "…",
  "input_schema": { "type": "object", "properties": { … } }
}
```

### `call_tool` return shape

- Success: `({"output": <str | list>}, False)`
- Upstream tool error: `({"error": <str | list>}, True)`
- Bridge-level error (timeout, unknown tool, no session, exception): `({"error": "<message>"}, True)`

### External dependencies (treated as black boxes)

- `scenecraft.oauth_client.SERVICES: dict[str, dict]` — at minimum provides `{"mcp_url": str}` per service key
- `scenecraft.oauth_client.get_valid_access_token(user_id: str, service: str) -> str | None`
- `mcp.ClientSession`, `mcp.client.sse.sse_client` — optional imports; ImportError is a degraded-mode path

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | Fresh `MCPBridge()` queried before any connect | `all_tools()` is `[]`; `has_tool(x)` is `False` | `fresh-bridge-is-empty` |
| 2 | `connect("remember", user)` with valid token, server returns 3 tools named `remember_a`, `remember_b`, `other` | Returns `True`; all three tool names kept as-is; `_tool_routing` maps each to `"remember"` | `connect-remember-keeps-names`, `tool-routing-records-service` |
| 3 | `connect("gmail", user)` with valid token, server returns tool `search` | Returns `True`; Claude-visible name is `gmail_search`; routing maps `gmail_search → gmail` | `connect-non-remember-prefixes-names` |
| 4 | `connect("gmail", user)`, upstream tool already named `gmail_inbox` | Name is NOT double-prefixed; stays `gmail_inbox` | `connect-skips-double-prefix` |
| 5 | `connect(service, user)` when service is not in `SERVICES` | Returns `False`; no session registered; logs warning | `connect-unknown-service-returns-false` |
| 6 | `connect(service, user)` when `get_valid_access_token` returns `None` | Returns `False`; no SSE attempt; no session registered | `connect-missing-token-returns-false` |
| 7 | `connect(service, user)` when `mcp` SDK import fails | Returns `False`; logs install hint; no raise | `connect-mcp-sdk-missing-returns-false` |
| 8 | SSE handshake takes > 10s | Returns `False`; `AsyncExitStack` is `aclose()`d; no partial session registered | `connect-sse-handshake-timeout` |
| 9 | `session.initialize()` takes > 15s | Returns `False`; stack closed; no session registered | `connect-init-timeout` |
| 10 | `list_tools()` takes > 15s | Returns `False`; stack closed; no session registered | `connect-list-tools-timeout` |
| 11 | Upstream tool has `description=None` and `inputSchema=None` | Emitted descriptor has `description=""` and `input_schema={"type":"object","properties":{}}` | `connect-fills-missing-description-and-schema` |
| 12 | `connect("remember", user)` called twice on same bridge | Second call returns `True` immediately, performs no SSE work, session unchanged | `connect-is-idempotent-per-service` |
| 13 | `all_tools()` after connects to remember + gmail | Returns remember tools then gmail tools, in insertion order | `all-tools-concatenates-in-order` |
| 14 | `call_tool("remember_search", {...})` on live session | Routes to remember session with upstream name `remember_search`; returns `({"output": ...}, False)` | `call-tool-success-remember`, `call-tool-does-not-strip-remember-prefix` |
| 15 | `call_tool("gmail_search", {...})` on live gmail session | Strips `gmail_` prefix; upstream called with `search`; returns success tuple | `call-tool-strips-service-prefix` |
| 16 | `call_tool("nope", {})` when name unknown | Returns `({"error": "unknown MCP tool: nope"}, True)`; no raise | `call-tool-unknown-name` |
| 17 | Routing exists but session was removed | Returns `({"error": "no live session for service: <svc>"}, True)` | `call-tool-no-session` |
| 18 | Upstream `call_tool` takes > 60s | Returns `({"error": "MCP tool <name> timed out"}, True)` | `call-tool-timeout` |
| 19 | Upstream `call_tool` raises `RuntimeError("boom")` | Returns `({"error": "MCP tool <name> failed: RuntimeError: boom"}, True)` | `call-tool-upstream-exception` |
| 20 | Upstream returns `isError=True` with one text item `"denied"` | Returns `({"error": "denied"}, True)` | `call-tool-upstream-iserror` |
| 21 | Upstream returns one text content item | Output is the bare string (not a list) | `call-tool-flattens-single-text` |
| 22 | Upstream returns two content items | Output is a list of the two flattened values, in order | `call-tool-preserves-multi-content-order` |
| 23 | Upstream returns non-text content supporting `model_dump()` | Item is converted via `model_dump()` | `call-tool-uses-model-dump` |
| 24 | Upstream returns non-text content with no `model_dump`, has `__dict__` | Item is converted to public-attrs dict | `call-tool-falls-back-to-vars` |
| 25 | Upstream returns opaque content (no `.text`, no `model_dump`, no `__dict__`) | Item becomes `{"value": str(item)}` | `call-tool-falls-back-to-str` |
| 26 | `close()` on a live bridge with 2 sessions | Both exit stacks `aclose()`d; `_sessions` and `_tool_routing` empty afterwards | `close-tears-down-all-sessions` |
| 27 | `close()` when one session's `aclose()` raises | Exception is logged, the other session still closes, bridge ends empty | `close-is-exception-safe` |
| 28 | `close()` on empty bridge | Completes without error | `close-empty-is-noop` |
| 29 | `handle_chat_connection` start | `MCPBridge` constructed synchronously; `_bg_connect_service("remember")` scheduled as a task; ws receive loop begins without awaiting connect | `chat-connect-is-fire-and-forget` |
| 30 | Remember token missing at chat start | Chat still accepts the first user message; `bridge.all_tools()` stays `[]`; no raise propagates to the ws loop | `chat-still-works-when-remember-unavailable` |
| 31 | Remember connect completes after first user message sent | The NEXT `_stream_response` invocation sees the discovered tools via `all_tools()` | `tools-become-available-on-next-stream` |
| 32 | `_bg_connect_service` hits an unexpected raise from `bridge.connect` | Exception caught and logged; chat loop unaffected | `bg-connect-swallows-exceptions` |
| 33 | OAuth token expires mid-session (target) | Bridge detects 401, force-refreshes token, reconnects session, retries call once; on second 401, degraded mode for that service | `call-tool-refreshes-on-401` |
| 34 | MCP server returns a tool with a malformed / non-conforming schema (target) | Tool skipped, warning logged with schema snippet; other tools still registered; connect returns True | `connect-skips-malformed-tool-schema` |
| 35 | Two non-remember services expose colliding tool names (target) | Universal `{service}__{tool}` prefix prevents collision regardless of upstream name | `universal-service-prefix-prevents-collision` |
| 36 | Remember query legitimately needs > 60s (target) | 300s default timeout accommodates long queries | `call-tool-300s-default-timeout` |
| 37 | Initial `connect` fails; chat session continues (target) | Exponential backoff retry (5s→60s cap, 5min total); on eventual success, `core__chat__mcp_tools_ready` emitted | `bg-connect-backoff-retry-then-ready-event` |
| 38 | Concurrent connect to same service (target) | `asyncio.Lock` per service serializes; second caller sees cached session | `concurrent-connect-serialized-by-lock` |
| 39 | Late-connect succeeds after WS loop already running (INV-4) | `core__chat__mcp_tools_ready` emitted on originating session's WS with `{service, tool_count}` | `late-connect-emits-tools-ready-event` |

---

## Behavior (step-by-step)

### `connect(service, user_id)`

1. Short-circuit: if `service in self._sessions`, return `True`.
2. Lookup `svc_cfg = SERVICES.get(service)`. If `None`, log and return `False`.
3. Call `get_valid_access_token(user_id, service)`. If falsy, log and return `False`.
4. Read `svc_cfg["mcp_url"]`.
5. Import `mcp.ClientSession` and `mcp.client.sse.sse_client`. On `ImportError`, log install hint and return `False`.
6. Create `stack = AsyncExitStack()`.
7. In a `try`:
   a. `read, write = await asyncio.wait_for(stack.enter_async_context(sse_client(url, headers={"Authorization": f"Bearer {token}"})), 10)`.
   b. `session = await asyncio.wait_for(stack.enter_async_context(ClientSession(read, write)), 10)`.
   c. `await asyncio.wait_for(session.initialize(), 15)`.
   d. `list_result = await asyncio.wait_for(session.list_tools(), 15)`; `raw_tools = getattr(list_result, "tools", []) or []`.
   e. For each tool `t`: compute `routed_name` per R6; append Claude descriptor; set `self._tool_routing[routed_name] = service`.
   f. Store `MCPSession(service, session, claude_tools, stack)` in `self._sessions`.
   g. Log `"{service}: N tools discovered"` and return `True`.
8. On any `Exception`: log `"connect({service}): failed — <Type>: <msg>"`, call `await stack.aclose()` inside a nested try (swallow errors), return `False`.

### `call_tool(name, arguments)`

1. `service = self._tool_routing.get(name)`. If `None`, return `({"error": "unknown MCP tool: <name>"}, True)`.
2. `sess = self._sessions.get(service)`. If `None`, return `({"error": "no live session for service: <service>"}, True)`.
3. `upstream_name = name`; if `service != "remember"` AND `name.startswith(f"{service}_")`, strip the prefix.
4. `result = await asyncio.wait_for(sess.session.call_tool(upstream_name, arguments=arguments or {}), 60)`.
   - On `asyncio.TimeoutError`: return `({"error": "MCP tool <name> timed out"}, True)`.
   - On any other `Exception`: return `({"error": "MCP tool <name> failed: <Type>: <msg>"}, True)`.
5. `is_error = bool(getattr(result, "isError", False))`. `content = getattr(result, "content", []) or []`.
6. Flatten each item per R14.
7. If `len(flat) == 1 and isinstance(flat[0], str)`: `output = flat[0]`, else `output = flat`.
8. Return `({"output": output}, False)` if not `is_error`, else `({"error": output}, True)`.

### `close()`

1. For each session (snapshot of `.values()`): try `await sess.exit_stack.aclose()`; on exception, log and continue.
2. Clear `self._sessions` and `self._tool_routing`.

### `handle_chat_connection` wiring (engine contract)

1. `bridge = MCPBridge()` — synchronous.
2. `asyncio.create_task(_bg_connect_service("remember"))`.
3. `_bg_connect_service` wraps `await bridge.connect(service, user_id=user_id)` in a `try/except Exception` that logs and returns.
4. The ws receive loop begins without awaiting the task.
5. Each `_stream_response` call reads `bridge.all_tools()` at the moment of invocation; if connect is not yet complete it sees `[]`.

---

## Acceptance Criteria

- [ ] `MCPBridge()` is cheap and I/O-free.
- [ ] All happy-path + bad-path rows in the Behavior Table pass as tests.
- [ ] `connect` never raises; every failure path logs and returns `False`.
- [ ] `close` never raises; bridge ends with empty state even when an `aclose()` raises.
- [ ] `call_tool` never raises; every failure path returns `(dict, True)`.
- [ ] Chat connection always proceeds to its ws loop regardless of MCP availability.
- [ ] All `undefined` rows are tracked in Open Questions, not implemented silently.
- [ ] No code path mutates the `arguments` dict passed into `call_tool`.

---

## Tests

### Base Cases

The core behavior contract: construction, connect happy + failure paths, tool routing, `call_tool` success and error paths, teardown, and the chat-handler wiring.

#### Test: fresh-bridge-is-empty (covers R1)
**Given**: A freshly constructed `MCPBridge`.
**When**: Query `all_tools()` and `has_tool("anything")`.
**Then**:
- **empty-tools**: `all_tools()` returns `[]`.
- **no-routing**: `has_tool("anything")` returns `False`.

#### Test: connect-remember-keeps-names (covers R2, R6, R7)
**Given**: Valid OAuth token for `remember`; stubbed `sse_client` + `ClientSession` where `list_tools` returns three tools `remember_a`, `remember_b`, `other` with `description="d"` and `inputSchema={"type":"object","properties":{"x":{"type":"string"}}}`.
**When**: `await bridge.connect("remember", "alice")`.
**Then**:
- **return-true**: returns `True`.
- **names-unchanged**: `all_tools()` contains names `remember_a`, `remember_b`, `other` (none prefixed).
- **schemas-passed-through**: each emitted `input_schema` equals the upstream `inputSchema`.
- **descriptions-passed-through**: each emitted `description` equals `"d"`.

#### Test: tool-routing-records-service (covers R7, R10)
**Given**: The bridge state after `connect-remember-keeps-names`.
**When**: Call `has_tool("remember_a")` and `has_tool("other")`.
**Then**:
- **routing-has-remember-a**: `has_tool("remember_a")` is `True`.
- **routing-has-other**: `has_tool("other")` is `True`.
- **routing-miss**: `has_tool("missing")` is `False`.

#### Test: connect-non-remember-prefixes-names (covers R6)
**Given**: Valid token for `gmail`; upstream `list_tools` returns one tool `search`.
**When**: `await bridge.connect("gmail", "alice")`.
**Then**:
- **claude-name-prefixed**: the emitted Claude name is `gmail_search`.
- **routing-prefixed**: `_tool_routing["gmail_search"] == "gmail"`.
- **no-bare-name-route**: `has_tool("search")` is `False`.

#### Test: connect-skips-double-prefix (covers R6)
**Given**: Upstream `list_tools` returns tool `gmail_inbox` from service `gmail`.
**When**: Connect to `gmail`.
**Then**:
- **no-double-prefix**: emitted name is `gmail_inbox`, not `gmail_gmail_inbox`.
- **routing-single**: `_tool_routing["gmail_inbox"] == "gmail"`.

#### Test: connect-unknown-service-returns-false (covers R3)
**Given**: `service="nope"` not in `SERVICES`.
**When**: `await bridge.connect("nope", "alice")`.
**Then**:
- **returns-false**: return value is `False`.
- **no-session**: `_sessions` is empty.
- **no-routing**: `_tool_routing` is empty.
- **no-raise**: no exception propagated.

#### Test: connect-missing-token-returns-false (covers R3, R5)
**Given**: `get_valid_access_token` returns `None`.
**When**: Connect to a known service.
**Then**:
- **returns-false**: return value is `False`.
- **no-sse-attempt**: `sse_client` was NOT called.
- **no-session-registered**: `_sessions` is empty.

#### Test: connect-fills-missing-description-and-schema (covers R7)
**Given**: Upstream tool with `description=None` and `inputSchema=None`.
**When**: Connect.
**Then**:
- **default-description**: emitted `description` is `""`.
- **default-schema**: emitted `input_schema` equals `{"type": "object", "properties": {}}`.

#### Test: connect-is-idempotent-per-service (covers R8)
**Given**: Bridge with remember already connected; a spy on `sse_client`.
**When**: `await bridge.connect("remember", "alice")` a second time.
**Then**:
- **returns-true**: second call returns `True`.
- **no-new-sse**: `sse_client` spy was not called again.
- **session-unchanged**: the remembered `MCPSession` object is the same instance.

#### Test: all-tools-concatenates-in-order (covers R9)
**Given**: Connect remember (2 tools), then gmail (1 tool).
**When**: Call `all_tools()`.
**Then**:
- **order**: returned list names are `[remember-tool-1, remember-tool-2, gmail_search]` in that order.
- **length**: returned list has length 3.

#### Test: call-tool-success-remember (covers R11, R14, R15)
**Given**: Live remember session; stubbed upstream `call_tool` returns a result with one text content item `"hello"` and `isError=False`.
**When**: `await bridge.call_tool("remember_search_memory", {"q": "x"})`.
**Then**:
- **upstream-name-kept**: upstream was called with name `remember_search_memory`.
- **arguments-passed**: upstream received `{"q": "x"}`.
- **success-tuple**: return is `({"output": "hello"}, False)`.

#### Test: call-tool-does-not-strip-remember-prefix (covers R11)
**Given**: Same as above.
**When**: Call `bridge.call_tool("remember_foo", {})`.
**Then**:
- **upstream-name-not-stripped**: upstream received `remember_foo`, not `foo`.

#### Test: call-tool-strips-service-prefix (covers R11)
**Given**: Live gmail session; tool registered as `gmail_search`.
**When**: `await bridge.call_tool("gmail_search", {"q": "x"})`.
**Then**:
- **prefix-stripped**: upstream was called with name `search`.
- **arguments-passed**: upstream received `{"q": "x"}`.

#### Test: call-tool-unknown-name (covers R13)
**Given**: Bridge with no matching routing for `nope`.
**When**: `await bridge.call_tool("nope", {})`.
**Then**:
- **is-error-true**: second element of tuple is `True`.
- **error-message**: first element equals `{"error": "unknown MCP tool: nope"}`.
- **no-raise**: no exception.

#### Test: call-tool-no-session (covers R13)
**Given**: `_tool_routing["x"] = "svc"` but `_sessions` does not contain `"svc"` (simulate forced removal).
**When**: `await bridge.call_tool("x", {})`.
**Then**:
- **is-error-true**: `True`.
- **error-message**: first element equals `{"error": "no live session for service: svc"}`.

#### Test: call-tool-upstream-exception (covers R13)
**Given**: Upstream `call_tool` raises `RuntimeError("boom")`.
**When**: Dispatch via bridge.
**Then**:
- **is-error-true**: `True`.
- **error-message-format**: error string contains `RuntimeError: boom` and the Claude-visible tool name.
- **no-raise**: bridge `call_tool` itself did not raise.

#### Test: call-tool-upstream-iserror (covers R15)
**Given**: Upstream returns `isError=True`, content `[text="denied"]`.
**When**: Dispatch.
**Then**:
- **is-error-true**: second element is `True`.
- **error-payload**: first element equals `{"error": "denied"}`.

#### Test: call-tool-flattens-single-text (covers R14)
**Given**: Upstream returns one text item `"hello"`.
**When**: Dispatch.
**Then**:
- **bare-string**: `output` is the string `"hello"`, not a list.

#### Test: close-tears-down-all-sessions (covers R16)
**Given**: Two live sessions, each with a tracked `AsyncExitStack` spy.
**When**: `await bridge.close()`.
**Then**:
- **both-aclose-called**: both `aclose()` spies were awaited once.
- **sessions-empty**: `_sessions` is empty afterwards.
- **routing-empty**: `_tool_routing` is empty afterwards.

#### Test: chat-connect-is-fire-and-forget (covers R18, R19)
**Given**: `handle_chat_connection` invoked against a ws stub; `bridge.connect` is stubbed to sleep 2s before returning `True`.
**When**: Observe timing between handler entry and the first `ws.recv()` call.
**Then**:
- **loop-starts-immediately**: the first `ws.recv()` call occurs before `bridge.connect` completes (bounded e.g. by < 200ms).
- **connect-scheduled**: exactly one background task was created for remember connect.
- **all-tools-empty-initially**: while the bg task is still pending, `bridge.all_tools()` returns `[]`.

#### Test: chat-still-works-when-remember-unavailable (covers R20)
**Given**: `bridge.connect` returns `False` (no token).
**When**: Chat handler runs and receives a user message.
**Then**:
- **no-raise-in-loop**: ws receive loop did not raise.
- **tools-empty**: `bridge.all_tools()` is `[]` when `_stream_response` is invoked.
- **stream-invoked**: `_stream_response` was still invoked for the user message.

#### Test: bg-connect-swallows-exceptions (covers R20)
**Given**: `bridge.connect` is patched to raise `RuntimeError("unexpected")`.
**When**: `_bg_connect_service("remember")` runs to completion.
**Then**:
- **no-raise-propagated**: task completes without an unhandled exception.
- **chat-loop-unaffected**: ws receive loop is still running.

### Edge Cases

Boundaries, concurrency, content-shape corner cases, and teardown resilience.

#### Test: connect-mcp-sdk-missing-returns-false (covers R3)
**Given**: Import of `mcp` raises `ImportError`.
**When**: Connect.
**Then**:
- **returns-false**: `False`.
- **log-hint**: a log line mentioning `pip install` or the `[ai]` extra is emitted.
- **no-raise**: no exception propagated.

#### Test: connect-sse-handshake-timeout (covers R4, R17)
**Given**: `sse_client` enter hangs forever.
**When**: Connect (bounded at 10s — test uses a fake clock or sets the timeout lower).
**Then**:
- **returns-false**: `False`.
- **stack-aclosed**: the `AsyncExitStack` `aclose()` was awaited.
- **no-session**: no `MCPSession` registered.

#### Test: connect-init-timeout (covers R4, R17)
**Given**: `session.initialize()` hangs forever.
**When**: Connect.
**Then**:
- **returns-false**: `False`.
- **stack-aclosed**: stack aclose() awaited.

#### Test: connect-list-tools-timeout (covers R4, R17)
**Given**: `session.list_tools()` hangs forever.
**When**: Connect.
**Then**:
- **returns-false**: `False`.
- **stack-aclosed**: stack aclose() awaited.
- **no-tools-registered**: `_tool_routing` has no entries for this service.

#### Test: call-tool-timeout (covers R12)
**Given**: Upstream `call_tool` hangs forever.
**When**: `await bridge.call_tool("remember_x", {})` (with test-shortened timeout).
**Then**:
- **returns-error-tuple**: return is `({"error": "MCP tool remember_x timed out"}, True)`.
- **no-raise**: no exception propagated.

#### Test: call-tool-preserves-multi-content-order (covers R14)
**Given**: Upstream returns `[text="a", text="b"]`.
**When**: Dispatch.
**Then**:
- **output-is-list**: `output` is `["a", "b"]`.
- **order-preserved**: order matches upstream.

#### Test: call-tool-uses-model-dump (covers R14)
**Given**: Upstream returns one non-text item exposing `model_dump()` returning `{"k": 1}` and no `.text`.
**When**: Dispatch.
**Then**:
- **model-dump-used**: `output` equals `{"k": 1}` (single-item not unwrapped because not a string).

#### Test: call-tool-falls-back-to-vars (covers R14)
**Given**: Non-text item with no `model_dump`, public attrs `{"a": 1, "_hidden": 2}`.
**When**: Dispatch.
**Then**:
- **public-only**: output item equals `{"a": 1}` (underscore-prefixed excluded).

#### Test: call-tool-falls-back-to-str (covers R14)
**Given**: Opaque item whose `str(item)` is `"opaque"` and no `model_dump`/`__dict__`.
**When**: Dispatch.
**Then**:
- **value-wrapped**: output item equals `{"value": "opaque"}`.

#### Test: close-is-exception-safe (covers R16)
**Given**: Two live sessions; first session's `exit_stack.aclose()` raises `RuntimeError`.
**When**: `await bridge.close()`.
**Then**:
- **no-raise-propagated**: `close()` does not raise.
- **second-still-closed**: the second session's `aclose()` was awaited.
- **state-cleared**: `_sessions` and `_tool_routing` are empty.

#### Test: close-empty-is-noop (covers R16)
**Given**: Fresh `MCPBridge`.
**When**: `await bridge.close()`.
**Then**:
- **no-raise**: completes cleanly.
- **state-empty**: `_sessions`, `_tool_routing` remain empty.

#### Test: tools-become-available-on-next-stream (covers R19)
**Given**: Chat handler started; bg remember connect pending.
**When**: (1) First `_stream_response` invocation observes `bridge.all_tools()` as `[]`; (2) bg task finishes successfully registering two tools; (3) second `_stream_response` invocation observes `bridge.all_tools()`.
**Then**:
- **first-empty**: first observation is `[]`.
- **second-populated**: second observation has length 2.
- **no-restart-needed**: no chat reconnect was required between the two observations.

#### Test: call-tool-accepts-none-arguments (covers R11)
**Given**: Live session.
**When**: `await bridge.call_tool("remember_x", None)`.
**Then**:
- **upstream-receives-empty-dict**: upstream `call_tool` received `arguments={}`, not `None`.

#### Test: call-tool-does-not-mutate-arguments (negative — covers R11)
**Given**: `args = {"k": "v"}`, live session.
**When**: `await bridge.call_tool("remember_x", args)`.
**Then**:
- **arguments-unmutated**: `args` is still `{"k": "v"}` afterwards (no keys added, none removed).

#### Test: call-tool-refreshes-on-401 (covers R21 target)
**Given**: Live remember session; upstream `call_tool` raises an auth error (simulated 401) on first attempt; `get_valid_access_token(user_id, "remember", force_refresh=True)` returns a new valid token; reconnect succeeds; retry returns one text item `"ok"`.
**When**: `await bridge.call_tool("remember_search", {})`.
**Then**:
- **refresh-attempted**: `get_valid_access_token` was called with `force_refresh=True` exactly once.
- **session-rebuilt**: the old `AsyncExitStack` was `aclose()`d and a new one entered with the new token.
- **retry-succeeds**: return is `({"output": "ok"}, False)`.
- **no-raise**: no exception propagated.

#### Test: call-tool-degraded-on-second-401 (covers R21 target)
**Given**: Upstream raises 401 on first call; token refresh succeeds; retry also raises 401.
**When**: Bridge processes the call.
**Then**:
- **service-hidden**: tools for the failing service are removed from `all_tools()`.
- **routing-cleared**: routing entries for that service are purged.
- **error-tuple**: return is `({"error": "MCP tool <name> auth failed after refresh"}, True)`.

#### Test: connect-skips-malformed-tool-schema (covers R22 target)
**Given**: Upstream `list_tools` returns 3 tools: `{name: "good", inputSchema: {...}}`, `{name: None, ...}`, `{name: "bad", inputSchema: "not-a-dict"}`.
**When**: Connect runs.
**Then**:
- **returns-true**: connect returns `True`.
- **only-valid-registered**: `all_tools()` contains only `good` (after any service-prefix rule).
- **warnings-logged**: stderr has one warning per skipped tool with a snippet of the offending schema.

#### Test: universal-service-prefix-prevents-collision (covers R23 target)
**Given**: Post-transition world — `remember` adopts the `{service}__` prefix. Services `remember` and `gmail` both expose an upstream tool named `search`.
**When**: Both connect.
**Then**:
- **remember-prefixed**: emitted Claude name is `remember__search`.
- **gmail-prefixed**: emitted Claude name is `gmail__search`.
- **no-collision**: `has_tool("remember__search")` and `has_tool("gmail__search")` are both True; `has_tool("search")` is False.

#### Test: call-tool-300s-default-timeout (covers R24 target)
**Given**: A live session; upstream `call_tool` completes at the 250s mark.
**When**: `await bridge.call_tool("remember_search", {})`.
**Then**:
- **completes-successfully**: return is `({"output": ...}, False)`.
- **default-timeout-300s**: inspection of the `asyncio.wait_for` timeout parameter inside `call_tool` reads `300` seconds.

#### Test: bg-connect-backoff-retry-then-ready-event (covers R25 target, R27, INV-4)
**Given**: `bridge.connect("remember", user)` returns `False` on first 3 attempts, then `True` on the 4th (stubbed). A WS session is open.
**When**: `_bg_connect_service("remember")` runs with 5s→60s backoff (fake clock).
**Then**:
- **retries-observed**: exactly 4 attempts; sleep durations `[5, 10, 20]` (doubling, capped at 60s).
- **wall-clock-bounded**: total retry window ≤ 5 minutes.
- **ready-event-emitted**: a WS frame `{"event": "core__chat__mcp_tools_ready", "service": "remember", "tool_count": <n>}` is sent to the originating session's WS exactly once after the 4th attempt succeeds.

#### Test: concurrent-connect-serialized-by-lock (covers R26 target)
**Given**: Two coroutines simultaneously call `bridge.connect("remember", user)`. Upstream SSE handshake takes 1s (mock).
**When**: Both awaited in parallel.
**Then**:
- **lock-acquired**: per-service `asyncio.Lock` serializes; SSE handshake occurs once, not twice.
- **one-session-registered**: `_sessions["remember"]` contains exactly one `MCPSession`.
- **both-return-true**: both coroutines observe `True` as return value.

#### Test: late-connect-emits-tools-ready-event (covers R27 target, INV-4)
**Given**: Chat WS loop has processed one user message; `_bg_connect_service("remember")` is still pending. Connect then completes, discovering 5 tools.
**When**: The bg task resolves successfully.
**Then**:
- **event-emitted**: exactly one WS frame with `"event": "core__chat__mcp_tools_ready"`, `"service": "remember"`, `"tool_count": 5`.
- **scoped-to-origin-session**: event sent only to the session that created this `MCPBridge`; other concurrent sessions do NOT receive it.
- **subsequent-stream-sees-tools**: the next `_stream_response` call reads 5 tools via `all_tools()`.

#### Test: negative-no-shared-lock-across-services (covers INV-1, R26)
**Given**: Two coroutines — one calling `connect("remember", user_A)` and one calling `connect("gmail", user_B)` — start simultaneously.
**When**: Both run.
**Then**:
- **independent-locks**: the per-service locks do NOT block each other; both SSE handshakes proceed in parallel.
- **both-sessions-registered**: `_sessions` ends with both entries.
- **invariant-INV-1**: per INV-1, the `asyncio.Lock` is scoped per `(service)` within a single-user bridge; cross-user bridges are per-WS and never share state.

#### Test: no-concurrency-primitives-in-bridge (negative — covers R1, R2)
**Given**: Source inspection of `MCPBridge`.
**When**: Enumerate bridge attributes and usage.
**Then**:
- **no-locks**: no `asyncio.Lock`, `Semaphore`, or similar is present in the bridge itself (the bridge relies on single-caller usage from the chat handler; concurrency safety across multiple simultaneous `connect` calls to the same service is NOT guaranteed — see OQ-6).
- **note**: this assertion documents intent. If a future implementer adds locks, they must update this spec.

---

## Non-Goals

- **Reconnect / retry of a failed `connect`** — today's behavior is "missed tools until the next chat session"; any retry policy is an explicit OQ (OQ-5), not implemented.
- **Token refresh mid-session** — the bridge does not re-fetch tokens after `connect`; behavior on mid-session expiry is an OQ (OQ-1).
- **Tool-name collisions between MCP bridge tools and plugin-dispatched tools** — resolved upstream in the chat dispatcher (plugin wins, per user note). The bridge itself does not deduplicate against plugin tools.
- **Concurrent `connect` calls for the same service from multiple coroutines** — not exercised in practice; see OQ-6.
- **Metrics / observability** — only stderr logs today.
- **Typed Python interface** — `session: Any` by design (lazy import).

---

## Transitional Behavior

Per INV-8, Requirements R21–R27 describe the **target-ideal** MCP bridge contract. Until the FastAPI refactor milestone lands, the following **transitional** behavior ships today:

- **No token refresh**: tokens are fetched once at `connect` and never re-read. An upstream 401 mid-session becomes a single `{"error": ...}` tuple from `call_tool`; the session stays registered in a broken state. Regression-locked by today's `call-tool-upstream-exception` test. Target (R21): detect 401, force-refresh, reconnect, retry once; degrade on second failure.
- **No schema validation on tool discovery**: tools are accepted as-is from `list_tools()`; `description=None` and `inputSchema=None` are filled with defaults (R7), but other malformations (non-string name, non-dict schema) reach Claude unchanged. Target (R22): validate during discovery, skip malformed with a warning.
- **`remember` unprefixed; others `<service>_<tool>` single-underscore**: per R6, `remember` tools pass through unchanged, other services use `{service}_{tool}` single-underscore. Regression-locked by `connect-remember-keeps-names`, `connect-non-remember-prefixes-names`. Target (R23): universal `{service}__{tool}` double-underscore for all services; `remember` retains current unprefixed names for one release cycle as a transitional exception.
- **60s `call_tool` timeout**: per R12, the default is 60s and legitimate long Remember queries time out. Regression-locked by `call-tool-timeout`. Target (R24): raise default to 300s.
- **No retry on initial connect failure**: if `_bg_connect_service` fails, the chat session runs without MCP tools until the next chat reconnect. No backoff, no late-ready event. Regression-locked by `chat-still-works-when-remember-unavailable`. Target (R25 + R27): exp backoff 5s→60s over 5min; on success emit `core__chat__mcp_tools_ready`.
- **No per-service lock on connect**: two concurrent calls to `connect(service, user)` race between the early-return check and the eventual session insert. In practice only `_bg_connect_service` calls this, once. Regression-locked by `no-concurrency-primitives-in-bridge`. Target (R26): per-service `asyncio.Lock`.
- **No `core__chat__mcp_tools_ready` event**: per-INV-4, all new events are `core__chat__*` namespaced; currently the bridge emits no late-ready signal. Target (R27): event emitted on late-connect success.

**Migration sequence** to the target:
1. Bump `call_tool` default timeout 60s → 300s (R24) — trivial, non-breaking.
2. Add per-service `asyncio.Lock` to `connect` (R26).
3. Add backoff retry + `core__chat__mcp_tools_ready` emission in `_bg_connect_service` (R25, R27).
4. Wire 401 detection + token refresh in `call_tool` (R21).
5. Add schema validation + skip-on-malformed in `list_tools` processing (R22).
6. Migrate `remember` tools to the universal `{service}__` prefix (R23) — coordinate with client-side tool-resolution code for one release cycle of back-compat.

---

## Open Questions

### Resolved

- **OQ-1 (OAuth token expiry mid-session)** — **Resolved** (fix): detect 401 from MCP server; re-fetch token via OAuth refresh flow; retry the call once. On refresh failure, degraded-mode (tools hidden). See R21.
- **OQ-2 (malformed tool schema)** — **Resolved** (fix): skip that tool, log warning with schema snippet; bridge continues with valid tools; connect returns True. See R22.
- **OQ-3 (cross-service tool name collision)** — **Resolved** (fix): namespace everything as `{service}__{tool}` (double-underscore). Remember tools stay un-prefixed for back-compat through one release cycle; future services MUST use the prefix. See R23.
- **OQ-4 (legitimate long Remember queries)** — **Resolved** (fix): raise `call_tool` timeout from 60s to 300s. Matches elicitation timeout. See R24.
- **OQ-5 (initial connect failure + long-lived session)** — **Resolved** (fix): retry with exponential backoff up to 5 min total. On eventual success, emit `core__chat__mcp_tools_ready` (matches chat-pipeline OQ-5 resolution). See R25, R27.
- **OQ-6 (concurrent connect same service)** — **Resolved** (fix): idempotent — `asyncio.Lock` per service prevents concurrent connects; second caller awaits the first's result. See R26.

### Deferred

_(none — all OQs resolved in the 2026-04-27 pass)_

### Historical (retained for audit trail)

#### OQ-1: OAuth token expiry mid-session
The bridge fetches a token once in `connect` and holds the SSE session open for the chat lifetime. If the Remember access token expires after connect, does the bridge:
(a) silently continue and let the next `call_tool` fail with an upstream 401,
(b) detect upstream 401 and attempt a single token refresh + session reconnect, or
(c) tear the session down on first auth failure so the next chat reconnect re-establishes it?
**Today**: path (a) — any upstream failure becomes a one-shot `{"error": ...}` and the session stays registered. The user likely wants at least (c). Needs a decision before this spec can close rows #33.

#### OQ-2: Malformed tool schema from MCP server
`list_tools()` returns tools with `name`, `description`, `inputSchema`. The current code is tolerant of `description=None` and `inputSchema=None` but not of, e.g., `name=None`, a non-string name, or an `inputSchema` that is not a dict (wrong JSON Schema shape, wrong type). Should the bridge:
(a) skip malformed tools and keep the valid ones,
(b) hard-fail the whole connect (return `False`) on any malformed tool,
(c) accept them as-is and let Claude reject them?
**Today**: (c) — no validation. Row #34.

#### OQ-3: Collision across two non-remember services
The prefixing rule makes cross-service collisions impossible *by name* (every non-remember tool gets its service prefix). But what if service `gmail` exposes `gmail_search` and service `gdrive` exposes `gdrive_search`? That's fine. What if `gmail` exposes a raw tool called `gdrive_something` (unlikely but legal)? Today the bridge writes `_tool_routing["gdrive_something"] = "gmail"` since it already starts with `gdrive_` — but it is actually a gmail tool. Should the prefix rule be: "prefix unless it already starts with the service's OWN prefix" (current), or "always prefix with the owning service's name" (stricter)? Row #35.

#### OQ-4: Legitimate long Remember queries
The 60s `call_tool` timeout is hard-coded. Some Remember queries (deep-search, large summarization) may legitimately take > 60s. Options:
(a) bump the default,
(b) make it per-tool configurable from the upstream tool metadata,
(c) stream progress to the caller,
(d) leave as-is and accept that long queries fail.
**Today**: (d). Row #36.

#### OQ-5: Initial connect failure + long-lived chat session
If `_bg_connect_service("remember")` fails on startup (network blip, token refresh race), the chat session runs for hours without Remember tools and with no automatic retry. Options:
(a) leave as-is (next chat reconnect tries again),
(b) schedule a delayed retry (e.g. exponential backoff capped at N attempts),
(c) expose a "reconnect MCP" command from the UI.
**Today**: (a). Row #37.

#### OQ-6: Concurrent connect to the same service
`connect` is not guarded by a lock. Two concurrent calls for the same service race between the `if service in self._sessions` check and the eventual insert, potentially creating two live SSE sessions where only one is reachable via `_sessions`. In practice only `_bg_connect_service` calls this today, and only once. If the surface ever expands (e.g. a UI-triggered reconnect), does the bridge need an `asyncio.Lock` per service? Flagged by `no-concurrency-primitives-in-bridge`.

---

## Related Artifacts

- Source: `src/scenecraft/mcp_bridge.py`
- Caller: `src/scenecraft/chat.py` — `handle_chat_connection`, `_bg_connect_service`
- OAuth client: `src/scenecraft/oauth_client.py` — `SERVICES`, `get_valid_access_token`
- Audit: `agent/reports/audit-2-architectural-deep-dive.md` §1B, unit 8
- Related specs: `agent/specs/local.openapi-tool-codegen.md` (adjacent tool-surface concerns), future `local.chat-pipeline.md` (engine chat streaming loop — unit 13 in the audit)

---

**Namespace**: local
**Spec**: engine-mcp-bridge
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft
