# Task 64: Checkpoints + undo + chat + plugin catch-all + 404 handler

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.fastapi-migration`](../../specs/local.fastapi-migration.md) — R4, R25, R28, R56
**Estimated Time**: 4–6 hours
**Dependencies**: T57, T58, T59
**Status**: Not Started

---

## Objective

Port the remaining route clusters: checkpoints (structural — `checkpoint` create is locked), undo/redo, chat history, and the **plugin POST catch-all** (registered last). Wire the final unknown-route 404 handler and the response-shape parity crawl that verifies the end-to-end route surface.

---

## TDD Plan

Capture parity fixtures for the remaining routes, including plugin-registered POST routes (with a dummy plugin). Write tests for the plugin catch-all ordering, the builtin-beats-plugin precedence, and the unknown-route handler. Implement routers until the full response-shape crawl passes across every router from T60–T64.

---

## Steps

### 1. Pydantic models

- `CheckpointCreateBody`, `CheckpointRestoreBody`, `CheckpointDeleteBody`
- `UndoBody`, `RedoBody`
- `ChatHistoryQuery` (limit)
- No model for plugin catch-all (accepts arbitrary dict)

### 2. Routers

#### `routers/checkpoints.py`

- `GET /api/projects/{name}/checkpoints` → `list_checkpoints_endpoint` (rename-avoid the chat tool `list_checkpoints`; **or** align: make operationId `list_checkpoints` ✓)
- `GET /api/projects/{name}/undo-history` → `get_undo_history`
- `POST /api/projects/{name}/checkpoint` → **`operation_id="checkpoint"`** 🔧 (chat tool, structural 🔒)
- `POST /api/projects/{name}/checkpoint/restore` → **`operation_id="restore_checkpoint"`** 🔧 (chat tool)
- `POST /api/projects/{name}/checkpoint/delete` → `delete_checkpoint`
- `POST /api/projects/{name}/undo` → `undo`
- `POST /api/projects/{name}/redo` → `redo`

🔧 operationIds must match chat tool names.

#### `routers/chat.py`

- `GET /api/projects/{name}/chat` → `get_chat_history` (supports `?limit=50`)

(`sql_query` chat tool has NO REST equivalent today and doesn't need one — it runs read-only SQL in-process. For T67 annotation, we'll add an `operationId="sql_query"` to a new endpoint: **`POST /api/projects/{name}/sql/query`** → `sql_query`. Add it here if scope allows, or defer to T67 as part of the tool-alignment audit.)

#### `routers/isolate_vocals.py` (plugin tool — `isolate_vocals__run` chat tool)

The chat tool `isolate_vocals__run` maps to a plugin-registered route today (`/api/projects/{name}/plugins/isolate_vocals/run`). Leave this to the plugin catch-all; T67 will tag the plugin operation with `x-tool`. No new router needed.

#### `routers/plugins.py` — plugin catch-all (registered LAST)

```python
router = APIRouter(tags=["plugins"])

@router.post(
    "/api/projects/{name}/plugins/{plugin}/{rest:path}",
    operation_id="plugin_dispatch",
    summary="Dispatch a plugin-registered POST route",
    include_in_schema=False,  # not interesting in OpenAPI — or set True per taste
)
async def plugin_dispatch(
    name: str,
    plugin: str,
    rest: str,
    request: Request,
    user: User = Depends(current_user),
):
    project_dir = _resolve_project_dir(request, name)
    body = await request.json() if request.headers.get("content-length") else {}
    from scenecraft.plugin_host import PluginHost
    try:
        result = PluginHost.dispatch_rest(
            f"/api/projects/{name}/plugins/{plugin}/{rest}",
            project_dir, name, body,
        )
    except Exception as e:
        raise ApiError("PLUGIN_ERROR", str(e), 500)
    if result is None:
        raise ApiError("NOT_FOUND", f"No route: POST /api/projects/{name}/plugins/{plugin}/{rest}", 404)
    return result
```

**Registration order in `app.py`:** plugin catch-all last. Every built-in router included before `app.include_router(plugins_router)`.

### 3. Unknown-route handler

Per R28, any unmatched route returns `{"error": "NOT_FOUND", "message": "No route: <METHOD> <path>"}` at 404. FastAPI's default 404 returns `{"detail": "Not Found"}` — override.

Install:
```python
@app.exception_handler(404)
async def _404_handler(request, exc):
    return JSONResponse(
        {"error": "NOT_FOUND", "message": f"No route: {request.method} {request.url.path}"},
        status_code=404,
    )
```

(Already stubbed in T58; verify it's still catching both the generic 404 and `HTTPException(404)` from routes. Adjust if not.)

### 4. Response-shape parity crawl

Create `tests/test_response_shape_parity_crawl.py`:
- Loads every fixture under `tests/fixtures/parity/*.json`.
- For each: issues the request against `TestClient(app)` and diffs the response.
- Diff rules:
  - Status code must match exactly.
  - JSON keys must match at every level of nesting.
  - Values must match, EXCEPT for fields tagged volatile (timestamps within 1s, server-generated IDs can differ but type must match).
  - Unknown volatile fields: fail; fixture must be updated or field tagged.

This is the R29 "response-shape-parity-crawl" test.

### 5. Tests to Pass

- `plugin_route_dispatches` — register a dummy plugin that handles `POST /api/projects/{name}/plugins/dummy/ping` returning `{"ok": True, "data": 42}`; hit the route; assert 200 + body + `PluginHost.dispatch_rest` was called with the full path.
- `plugin_error_500` — dummy handler raises `RuntimeError("nope")`; hit the route; expect 500, `PLUGIN_ERROR`, message contains `nope`.
- `plugin_none_returns_404` — dummy handler returns None; hit the route; expect 404 `NOT_FOUND`.
- `builtin_beats_plugin_catchall` — register a plugin that tries to shadow `/api/projects/{name}/plugins/x/add-keyframe` (pathologically); hit `/api/projects/{name}/add-keyframe` — confirm built-in handler runs, not the plugin. (This test mostly documents router order — real routes don't collide.)
- `unknown_route_404` — GET `/api/nope/nope/nope`; 404 + envelope.
- `response_shape_parity_crawl` — full crawl over every parity fixture from T60–T64.

---

## Verification

- [ ] Checkpoint, undo, redo, chat routes registered
- [ ] Checkpoint-related operationIds match chat tool names: `list_checkpoints`, `checkpoint`, `restore_checkpoint`
- [ ] Plugin catch-all registered LAST
- [ ] Unknown-route handler emits legacy envelope
- [ ] All 5 plugin/404/parity tests pass
- [ ] `response_shape_parity_crawl` is green across the entire T60–T64 route surface
- [ ] No business logic rewritten

---

## Tests Covered

`plugin-route-dispatches`, `plugin-error-500`, `plugin-none-returns-404`, `builtin-beats-plugin-catchall`, `unknown-route-404`, `response-shape-parity-crawl`.

---

## Notes

- The plugin catch-all's `include_in_schema=False` hides it from `/openapi.json`. This is fine — plugin routes are not chat-tool candidates by default. If a specific plugin wants to expose a chat tool, T67 can unmark `include_in_schema` and add `x-tool: true` on a per-plugin basis.
- `response_shape_parity_crawl` is the most comprehensive test in the migration. Run it last; it will catch any router gap introduced by T60–T63.
- The chat tool `sql_query` has no REST equivalent today. Add `POST /api/projects/{name}/sql/query` → `sql_query` here (thin wrapper over the existing `_exec_sql_query` body) so T67 has a route to annotate. Keep it auth-gated (it's a read-only SQL SELECT against the project DB).
