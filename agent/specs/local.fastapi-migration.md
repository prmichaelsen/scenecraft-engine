# Spec: FastAPI Migration

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-24
**Last Updated**: 2026-04-24
**Status**: Ready for Proofing

---

**Purpose**: Implementation-ready contract for replacing the hand-rolled `http.server.BaseHTTPRequestHandler`-based `api_server.py` (10,320 LOC, 164 routes) with a FastAPI/uvicorn implementation, preserving every route's exact path, response shape, status code, auth behavior, streaming semantics, and plugin compatibility, and emitting a valid `openapi.json` as a side effect.
**Source**: Interactive — decisions captured from chat on 2026-04-24 (see Key Design Decisions).

---

## Scope

### In-Scope

- A FastAPI application (`src/scenecraft/api/app.py`) + uvicorn runner replacing `api_server.py`
- All 164 existing REST routes ported with identical paths, methods, request parsing, response shapes, and status codes
- Pydantic request models per route (no more ad-hoc `self._read_json_body()`); Pydantic response models where the response is structured (file-streaming routes use raw `Response`/`StreamingResponse`)
- Auth moved into FastAPI `Depends`: cookie (HttpOnly, set by `GET /auth/login`), bearer token, OAuth authorization-code callback
- CORS via `fastapi.middleware.cors.CORSMiddleware`, replacing ad-hoc `_cors_headers()` calls
- Per-project structural-mutation lock preserved as a FastAPI dependency applied to the existing 10-route structural set
- Post-structural-mutation timeline validation preserved, with WS broadcast of warnings
- Streaming correctness preserved: Range requests on `GET /api/projects/{name}/files/{path}`, HEAD, and the JPEG hot path `/render-frame`
- Plugin POST route fallback preserved: `/api/projects/{name}/plugins/{plugin}/{rest:path}` catch-all dispatching to `PluginHost.dispatch_rest`
- `operationId` explicitly set on every route using the naming convention below
- `GET /openapi.json` exposed and validated against OpenAPI 3.1
- Entry-point cutover: `scenecraft.cli:main` launches uvicorn by default; `api_server.py` deleted in the same PR
- Existing test suite (897 tests across 61 files) passes unchanged against the new server

### Out-of-Scope (Non-Goals)

- Tool schema codegen — covered by `agent/specs/local.openapi-tool-codegen.md`
- Migrating `ws_server.py` — WebSocket server stays independent on port 8891
- Any new endpoints or behavior changes
- Performance tuning beyond parity (target: no measurable regression on `/render-frame` and `/files/*`; no new tuning)
- Client-side changes in the scenecraft frontend
- Feature-flag toggle between legacy and FastAPI — **hard cut** in one PR
- Removing stdlib `http.server` dependency from unrelated modules (`marker_server.py` etc.) — migration is scoped to `api_server.py` only
- Authorization model changes (same roles, same cookie scopes, same OAuth providers)
- Rewriting any business-logic module (`db.py`, `chat.py`, `generator.py`, `audio_intelligence.py`, etc.) — handlers are thin wrappers over existing functions

---

## Requirements

### Architecture

- **R1**: A new package `src/scenecraft/api/` contains the FastAPI app, models, dependencies, and routers. `api_server.py` is deleted in the same PR that merges this spec's implementation.
- **R2**: `src/scenecraft/api/app.py` exports a module-level `app: FastAPI` instance. The app is constructable by tests without starting uvicorn (fixtures call `TestClient(app)`).
- **R3**: `scenecraft.cli:main` starts the server via `uvicorn.run(app, host=..., port=...)`. The host, port, and work-dir arguments match the existing CLI (no breaking changes to the command line).
- **R4**: Routes are split into routers by domain under `src/scenecraft/api/routers/`. Minimum split:
  - `projects` (project CRUD, browse, meta, watched folders, narrative, workspace views, settings, ingredients, branches, checkout)
  - `auth` (login/logout, cookie handshake)
  - `oauth` (authorize, status, callback, disconnect)
  - `keyframes` (add/delete/update/duplicate/batch/restore/assign/style/label/suggest/enhance/escalate/unlink/extend-video)
  - `transitions` (delete/restore/split/trim/move/link-audio/update-*/generate-action/enhance-action/copy-style/duplicate-video/effects)
  - `audio_tracks` (tracks add/update/delete/reorder, track-effects, track-sends, send-buses, master-bus-effects)
  - `audio_clips` (add/add-from-pool/update/delete/batch-ops/align-detect/peaks)
  - `effects_curves` (effect-curves CRUD + batch, frequency-labels)
  - `rendering` (render-frame, render-state, render-cache, thumb, thumbnail, filmstrip, download-preview, mix-render-upload)
  - `pool` (list, add, import, upload, rename, tag, untag, gc, gc-preview, peaks, assign-pool-video, paste-group, insert-pool-item)
  - `candidates` (generate-keyframe-candidates, generate-transition-candidates, generate-slot-keyframe-candidates, unselected-candidates, video-candidates, promote-staged, generate-staged)
  - `checkpoints` (list, create, restore, delete, undo-history, undo, redo)
  - `markers` (add/update/remove, list, prompt-roster)
  - `chat` (chat history, escalate-prompts)
  - `files` (files bytes + HEAD, thumb, descriptions, beats, bin, ls, audio-intelligence stub)
  - `rules` (update-rules, reapply-rules — stubs preserved for client compat)
  - `plugins` (plugin POST catch-all, plugin GET listing if present)
  - `bench` (bench capture/upload/add/remove)
  - `misc` (config, markers, prompt-roster, save-as-still, import, section-settings, update-meta)
- **R5**: Router module names MUST use `snake_case` and live in `src/scenecraft/api/routers/`. Every router is included in `app` via `app.include_router(router)` in `app.py`.

### Route Contract

- **R6**: Every route in the current `api_server.py` (as enumerated by the `# METHOD /path` comments) is reachable at the same path with the same HTTP method in the new implementation. No path, method, or status code changes.
- **R7**: Every route has an explicit `operationId` of the form `<method>_<path_skeleton>` where `<path_skeleton>` is the path with parameter names preserved but slashes replaced by underscores and `api/` stripped. Examples:
  - `POST /api/projects/{name}/audio-tracks/add` → `operationId = "add_audio_track"` (override via `operation_id="..."` on the decorator — prefer imperative verbs)
  - `GET /api/projects/{name}/keyframes` → `operationId = "get_keyframes"`
  - `DELETE /api/projects/{name}/track-effects/{effect_id}` → `operationId = "delete_track_effect"`
  - `GET /api/projects/{name}/files/{file_path:path}` → `operationId = "get_project_file"`
  The 32 current chat tool names MUST be adopted verbatim as `operationId`s for their corresponding routes (this is a load-bearing constraint for the tool-codegen spec).
- **R8**: Response shape for every route is byte-identical to the current implementation for structured JSON responses. Field order in JSON is not required to match (JSON object key order is semantically irrelevant), but field names, types, and values MUST match.
- **R9**: Error envelope is preserved: any 4xx/5xx response (other than 204/304) MUST return JSON `{"error": "<CODE>", "message": "<human text>"}` where `<CODE>` is one of the existing codes (`BAD_REQUEST`, `NOT_FOUND`, `CONFLICT`, `UNAUTHORIZED`, `INTERNAL_ERROR`, `PLUGIN_ERROR`, etc.). Implementation uses a custom exception handler installed via `app.exception_handler(HTTPException)` and `app.exception_handler(Exception)`.
- **R10**: Request body parsing uses Pydantic models per operation. A model's extra-field policy is `ignore` (matches current permissiveness — the hand-rolled server reads only the keys it needs).
- **R11**: OPTIONS preflight returns `204` with CORS headers on every route (CORSMiddleware default).
- **R12**: HEAD on `/api/projects/{name}/files/{path:path}` returns 200 with `Content-Type`, `Content-Length`, `Accept-Ranges: bytes` and empty body, matching `api_server.py:2370` behavior. HEAD on any other route returns 405 unless FastAPI auto-adds it — it does not, and we do not add it for other routes.

### Auth

- **R13**: A single `current_user` dependency (`Depends(require_user)`) authenticates each request. It reads, in order: bearer token from `Authorization: Bearer <token>`, then session cookie. On failure it raises `HTTPException(401, detail="UNAUTHORIZED: Invalid or expired token")` which the exception handler converts to the standard error envelope.
- **R14**: Public routes (no auth): `GET /auth/login?code=...`, `GET /oauth/callback`, `GET /openapi.json`, `GET /docs`, `GET /redoc`. All other routes require authentication.
- **R15**: `GET /auth/login?code=<one-time-code>` exchanges the code for an HttpOnly cookie and returns the exact redirect response the legacy server returns (302 or HTML with JS, matching current behavior — see `_handle_auth_login`).
- **R16**: `POST /auth/logout` clears the session cookie via `Set-Cookie` with `Max-Age=0` and returns `{"ok": true}`, matching current behavior.
- **R17**: OAuth flows (`/api/oauth/{service}/authorize`, `/api/oauth/{service}/status`, `/api/oauth/{service}/disconnect`, `/oauth/callback`) preserve exact URL shapes and response bodies. Token storage and service lookup are unchanged.

### Structural Mutation Lock

- **R18**: A dependency `acquire_project_lock(name: str)` acquires `_get_project_lock(name)` before the handler runs and releases it in a `finally` after. It is attached only to routes in the structural set:
  - `add-keyframe`, `duplicate-keyframe`, `delete-keyframe`, `batch-delete-keyframes`, `restore-keyframe`
  - `delete-transition`, `restore-transition`
  - `split-transition`, `insert-pool-item`, `paste-group`
  - `checkpoint` (POST — the create variant, not list/restore/delete)
- **R19**: After a successful structural mutation, the timeline validator (`scenecraft.db.validate_timeline`) runs. If it returns warnings, they are:
  - Logged at most 10 per mutation
  - Broadcast via `ws_server.job_manager._broadcast({"type": "timeline_warning", "route": <route_name>, "warnings": [...]})`
  The validator runs INSIDE the lock scope; an exception in the validator is caught, logged, and MUST NOT fail the request.

### Streaming

- **R20**: `GET /api/projects/{name}/files/{path:path}` serves the file's bytes with `Content-Type` inferred from `mimetypes.guess_type`, falling back to `application/octet-stream`. The response sets `Accept-Ranges: bytes`.
- **R21**: If the request includes a `Range: bytes=<start>-<end>` header:
  - Response status is `206 Partial Content`
  - `Content-Range: bytes <start>-<end>/<file_size>` is set
  - Body contains exactly the bytes in `[start, end]` inclusive
  - Missing `<end>` means end-of-file; missing `<start>` is a suffix range (legacy server does not support suffix ranges — preserve that: reject suffix ranges with 416)
  - Streaming is chunked at 64 KiB (`min(65536, remaining)`) matching legacy
- **R22**: `/api/projects/{name}/files/{path:path}` MUST prevent path traversal: after resolving the full path, reject any path whose resolved location is not under `work_dir / project_name` with `404 NOT_FOUND`. Legacy check: `str(full_path).startswith(str(work_dir.resolve()))` — preserve this exact semantic.
- **R23**: `GET /api/projects/{name}/render-frame?t=<seconds>[&quality=<1-100>]` returns JPEG bytes with `Content-Type: image/jpeg`. The JPEG bytes for a given project state + `t` + `quality` MUST be byte-for-byte identical to the legacy implementation (same encoder call, same params). This is verified by a direct byte comparison against a legacy-captured fixture in tests.
- **R24**: Other streaming/binary endpoints (`/thumb/...`, `/thumbnail/...`, `/filmstrip`, `/download-preview`) return `Content-Type: image/*` or `video/*` with identical bytes to the legacy implementation.

### Plugin Routes

- **R25**: The catch-all `POST /api/projects/{name}/plugins/{plugin}/{rest:path}` is registered **last**, after all built-in POST routes. It:
  - Authenticates via the same `current_user` dependency
  - Resolves `project_dir` or returns `404 NOT_FOUND` with error envelope
  - Reads JSON body (empty dict if absent)
  - Calls `PluginHost.dispatch_rest(path, project_dir, project_name, body)`
  - Returns the dict result as JSON on success
  - Returns `500 PLUGIN_ERROR` with the exception message on handler exception
  - Returns `404 NOT_FOUND` with `No route: POST {path}` if `dispatch_rest` returns None

### Validation & Error Handling

- **R26**: Validation errors from Pydantic MUST return `400` with `{"error": "BAD_REQUEST", "message": "<human explanation of first error>"}` — NOT FastAPI's default `422` with the nested validation-error list. Implementation: custom `RequestValidationError` handler that flattens the first error into the legacy envelope.
- **R27**: Missing required field returns 400 with `message="Missing '<field>'"` matching the legacy error message where the legacy server explicitly checks. Where the legacy server does not explicitly check, Pydantic's flattened message is acceptable.
- **R28**: Unknown route returns `404 NOT_FOUND` with `{"error": "NOT_FOUND", "message": "No route: <METHOD> <path>"}` via a custom `404` handler on the app.

### OpenAPI Spec Emission

- **R29**: `GET /openapi.json` returns a valid OpenAPI 3.1 document. Validity is verified by `openapi-spec-validator` in a test.
- **R30**: Every path operation has `operationId`, `summary` (short imperative, <= 80 chars), and at least one response schema (may be `{}` for streaming routes).
- **R31**: `GET /docs` (Swagger UI) and `GET /redoc` (ReDoc) render the spec. These are enabled by default; not auth-gated (same as `openapi.json`).
- **R32**: The OpenAPI spec file path and schema surface are stable enough that a snapshot file (`tests/fixtures/openapi.snapshot.json`) can be committed and diffed in CI. Non-substantive diffs (FastAPI version bumps, description whitespace) are acceptable; structural diffs fail the snapshot test until regenerated.

### Entry Point & Build

- **R33**: `pyproject.toml` adds `fastapi>=0.110`, `uvicorn[standard]>=0.27`, `python-multipart>=0.0.9`, `openapi-spec-validator>=0.7` (dev).
- **R34**: `api_server.py` is deleted. Any imports of `make_handler` or `api_server` from other modules are redirected to the new app factory or removed. A `git grep api_server` in the migrated tree returns zero matches.
- **R35**: `scripts/` and CI invocations that launched the old server launch uvicorn instead, via either `scenecraft` CLI or `uvicorn scenecraft.api.app:app --host ... --port ...`.

---

## Interfaces / Data Shapes

### Python

```python
# src/scenecraft/api/app.py
from fastapi import FastAPI

app: FastAPI  # module-level; constructible without side effects

def create_app(work_dir: Path, *, enable_docs: bool = True) -> FastAPI: ...
```

```python
# src/scenecraft/api/deps.py
from fastapi import Depends, HTTPException, Request, Cookie, Header
from scenecraft.auth import authenticate_user, User

async def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    session: str | None = Cookie(default=None),
) -> User: ...

def project_dir(name: str, user: User = Depends(current_user)) -> Path: ...

async def project_lock(name: str):
    """Acquire the per-project structural lock; release in a try/finally."""
```

```python
# src/scenecraft/api/errors.py
from fastapi import HTTPException, status

class ApiError(HTTPException):
    def __init__(self, code: str, message: str, status_code: int = 400): ...

# Custom exception handler emits {"error": code, "message": message}
```

```python
# src/scenecraft/api/streaming.py
from starlette.responses import Response, StreamingResponse
from pathlib import Path

def file_response_with_range(path: Path, request) -> Response:
    """Return Response or StreamingResponse respecting Range headers. 206 on partial, 416 on suffix range."""
```

### Route signature shape (example)

```python
# src/scenecraft/api/routers/audio_tracks.py
from fastapi import APIRouter, Depends
from scenecraft.api.deps import current_user, project_dir, project_lock
from scenecraft.api.models.audio_tracks import AddAudioTrackBody, AddAudioTrackResponse

router = APIRouter(prefix="/api/projects/{name}", tags=["audio-tracks"])

@router.post(
    "/audio-tracks/add",
    operation_id="add_audio_track",
    summary="Add an audio track to a project",
    response_model=AddAudioTrackResponse,
    dependencies=[Depends(project_lock)],  # only when structural; this one is NOT structural
    openapi_extra={  # consumed by tool-codegen spec
        "x-tool": True,
        "x-tool-description": "...",
    },
)
async def add_audio_track(
    name: str,
    body: AddAudioTrackBody,
    proj: Path = Depends(project_dir),
) -> AddAudioTrackResponse:
    from scenecraft import db
    track_id = db.add_audio_track(proj, name=body.name, ...)
    return AddAudioTrackResponse(id=track_id, ...)
```

### Error envelope (unchanged)

```json
{"error": "BAD_REQUEST", "message": "Missing 'name'"}
```

### OpenAPI extensions used (consumed by the tool-codegen spec, not this one)

- `x-tool: boolean` — if true, the operation is surfaced as a chat tool
- `x-tool-description: string` — LLM-facing description (distinct from `summary`/`description`)
- `x-tool-name: string` — override for the chat tool name; defaults to `operationId`
- `x-destructive: boolean` — tool is gated by the destructive-confirmation flow

This spec only guarantees that FastAPI routes MAY carry these keys via `openapi_extra=`; their semantics are defined in `local.openapi-tool-codegen.md`.

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | Authenticated GET for a known JSON route | Returns 200 with same JSON shape as legacy | `get-route-parity`, `operation-id-set-for-every-route` |
| 2 | Authenticated POST for a known JSON route with valid body | Returns 200, side effects match legacy | `post-route-parity` |
| 3 | DELETE for an existing resource | Returns 200 (idempotent per M13 spec), empty body | `delete-route-parity` |
| 4 | DELETE for a non-existent resource | Returns 200 empty body (idempotent, preserving M13 semantics) | `delete-idempotent-parity` |
| 5 | Request missing session/bearer on a protected route | Returns 401 with `{"error": "UNAUTHORIZED", ...}` | `auth-required-returns-401` |
| 6 | Request with valid bearer header | Authenticates; handler runs | `bearer-auth-succeeds` |
| 7 | Request with valid session cookie | Authenticates; handler runs | `cookie-auth-succeeds` |
| 8 | POST with invalid JSON body | Returns 400 `BAD_REQUEST` | `invalid-json-returns-400` |
| 9 | POST with missing required field | Returns 400 `BAD_REQUEST` with legacy message | `missing-field-returns-400` |
| 10 | OPTIONS preflight on any route | Returns 204 with CORS headers | `options-preflight-204` |
| 11 | GET `/api/projects/{name}/files/{path}` without Range | Returns 200 with full file bytes and `Accept-Ranges: bytes` | `file-get-no-range` |
| 12 | GET `/api/projects/{name}/files/{path}` with valid `Range: bytes=0-999` | Returns 206 with correct `Content-Range` and 1000 bytes | `file-get-range-206` |
| 13 | GET files with out-of-bounds Range | Returns 416 `Range Not Satisfiable` | `file-get-range-416` |
| 14 | GET files with suffix range (`bytes=-100`) | Returns 416 (legacy does not support suffix) | `file-get-suffix-range-416` |
| 15 | HEAD `/api/projects/{name}/files/{path}` | Returns 200 with headers, empty body | `file-head-metadata-only` |
| 16 | GET files with `..` in path attempting traversal | Returns 404 | `file-traversal-rejected` |
| 17 | GET `/render-frame?t=X` | JPEG bytes byte-identical to legacy fixture | `render-frame-bytes-identical` |
| 18 | Two concurrent `POST /add-keyframe` on same project | Serialized by project lock; no duplicate bridges | `structural-lock-serializes` |
| 19 | Structural mutation succeeds with timeline warnings | WS broadcast emits `timeline_warning` with warnings list | `timeline-validator-runs-after-mutation` |
| 20 | Timeline validator raises unexpectedly | Mutation response still 200; error logged; no 500 | `validator-exception-non-fatal` |
| 21 | Plugin route `/api/projects/{name}/plugins/foo/bar` POST | Dispatched to `PluginHost.dispatch_rest`, returns 200 with dict | `plugin-route-dispatches` |
| 22 | Plugin handler raises | Returns 500 `PLUGIN_ERROR` with exception message | `plugin-error-500` |
| 23 | Plugin handler returns None | Returns 404 `NOT_FOUND` | `plugin-none-returns-404` |
| 24 | Unknown route | Returns 404 with legacy error envelope | `unknown-route-404` |
| 25 | `GET /openapi.json` | Returns valid OpenAPI 3.1 with every `operationId` set | `openapi-valid-3-1`, `every-route-has-operation-id` |
| 26 | `GET /docs` | Returns Swagger UI HTML | `swagger-ui-renders` |
| 27 | Uvicorn started via `scenecraft` CLI | Listens on configured host/port, same defaults as legacy | `cli-starts-uvicorn` |
| 28 | Full existing test suite against new server | All 897 tests pass | `legacy-test-suite-green` |
| 29 | Byte diff of response body for every structured route (legacy vs new) | Shapes identical (field-by-field equality, ignoring JSON key ordering) | `response-shape-parity-crawl` |
| 30 | WS server on port 8891 | Unaffected; starts and serves independently | `ws-server-independent` |
| 31 | `api_server.py` present after migration | File does not exist in repo | `legacy-server-deleted` |
| 32 | Validation error shape | 400 with `{"error": "BAD_REQUEST", "message": "..."}`, NOT FastAPI default 422 | `validation-envelope-legacy-shape` |
| 33 | OPTIONS on a route that doesn't exist | Returns 204 with CORS headers (CORSMiddleware default) | `options-always-204` |
| 34 | Cookie set via `/auth/login?code=...` | `Set-Cookie` HttpOnly, Secure, SameSite=Lax; redirect response matches legacy | `auth-login-sets-cookie-and-redirects` |
| 35 | `POST /auth/logout` | `Set-Cookie` with `Max-Age=0`; returns `{"ok": true}` | `auth-logout-clears-cookie` |
| 36 | OAuth callback with valid code | Exchanges for token, stores it, redirects to app | `oauth-callback-success` |
| 37 | OAuth callback with bad state | Returns 400 with error envelope | `oauth-callback-bad-state` |
| 38 | Performance: `/render-frame` p50 latency | Within 10% of legacy baseline over 100 calls | `render-frame-perf-no-regression` |
| 39 | Performance: `/files/*` range throughput | Within 10% of legacy baseline for a 100 MB file scrubbed in 1 MB ranges | `files-range-perf-no-regression` |
| 40 | Concurrent unrelated-project structural mutations | NOT serialized across projects (lock is per-project) | `structural-lock-is-per-project` |
| 41 | Client sends non-UTF8 path in `/files/{path:path}` | Returns 400 with error envelope | `invalid-path-encoding-400` |
| 42 | Server shutdown while a long `/download-preview` is streaming | Stream terminates cleanly, no uvicorn traceback in the log at error level | `graceful-shutdown-during-stream` |
| 43 | A route's Pydantic model rejects an unknown extra field | Field is ignored (extra=ignore matches legacy permissiveness) | `extra-fields-ignored` |
| 44 | CLI `--help` output | Unchanged from legacy (same flags, same descriptions) | `cli-help-unchanged` |
| 45 | Structural mutation raises before the lock is released | Lock is released; subsequent request succeeds | `lock-released-on-exception` |
| 46 | Existing chat-tool `_exec_*` paths that bypass HTTP | Still work — direct `db.*` calls are unaffected | `chat-exec-paths-unaffected` |
| 47 | OpenAPI operation-ID convention for 32 existing chat tools | `operationId` equals the chat tool name exactly | `chat-tool-operation-ids-match` |
| 48 | A route handler raises an unhandled exception | Returns 500 with `{"error": "INTERNAL_ERROR", "message": "..."}`, traceback logged server-side | `unhandled-exception-500-envelope` |
| 49 | CORS headers on every response | `Access-Control-Allow-Origin` set per CORS config, matching legacy policy | `cors-on-every-response` |
| 50 | Exact CORS origin allowlist | Matches the `_cors_headers` value set in `api_server.py` | `cors-origin-matches-legacy` |
| 51 | Multipart upload to `/pool/upload` | Succeeds with `python-multipart` installed; identical DB effect to legacy | `multipart-upload-parity` |
| 52 | Large multipart upload (>100 MB) | Streams to disk without loading fully into memory | `large-upload-streams` |
| 53 | Behavior of per-project lock under exception during timeline validation | Lock still released; request still 200 | `validator-exception-lock-released` |
| 54 | Legacy test fixture that creates `HTTPServer(make_handler(...))` | Replaced by `TestClient(app)`; same assertions pass | `test-client-replaces-http-server-fixture` |
| 55 | `/openapi.json` snapshot drift | CI fails on structural diff until fixture is regenerated | `openapi-snapshot-diff-flagged` |
| 56 | Plugin catch-all precedence | Built-in POST route wins over plugin catch-all when paths overlap | `builtin-beats-plugin-catchall` |
| 57 | Request to a deprecated route (e.g. `/version/commit`) | Still returns the legacy no-op response | `deprecated-noops-preserved` |
| 58 | Operations with no request body (e.g. `POST /undo`) | Tool-eligible and return 200 | `no-body-post-works` |
| 59 | `undefined` — should `HEAD` work on `/render-frame` for preload hints? | `undefined` | → [OQ-1](#open-questions) |
| 60 | `undefined` — should FastAPI's default `422` validation envelope be opt-in for new endpoints added after migration? | `undefined` | → [OQ-2](#open-questions) |

---

## Behavior

### Startup

1. `scenecraft` CLI parses args identically to today.
2. `create_app(work_dir)` returns a configured FastAPI app: CORS installed, exception handlers registered, routers included (in order: built-in routers, then plugin catch-all last), `current_user` dependency injected into every non-public route.
3. `uvicorn.run(app, host=..., port=...)` serves the app on the same host/port defaults as legacy.
4. `ws_server.start_ws_server(...)` is launched on its own thread/port as before — no change.

### Per-Request

1. CORSMiddleware handles preflight; for non-OPTIONS, delegates to route.
2. Route match; path parameters validated; body parsed by Pydantic model.
3. `current_user` dependency resolves the session (bearer or cookie). On failure, `HTTPException(401)` → error envelope.
4. If route is structural, `project_lock` dependency acquires the per-project lock.
5. Handler runs; returns either a Pydantic model, a dict, or a raw `Response`/`StreamingResponse`.
6. If structural, `finally` block runs timeline validator and broadcasts warnings; lock released unconditionally.
7. Exception handler converts any unhandled exception to `500 INTERNAL_ERROR`; Pydantic validation errors to `400 BAD_REQUEST`.
8. Response emitted with CORS headers.

### File Range Request

1. GET `/files/{path:path}` with `Range: bytes=X-Y`.
2. Parse range; if suffix or invalid, return 416.
3. Open file at `full_path`; seek to X.
4. Stream chunks of `min(65536, remaining)` bytes until `end-start+1` bytes sent.
5. Set status 206, `Content-Range: bytes X-Y/size`, `Accept-Ranges: bytes`.

### Plugin Route

1. `POST /api/projects/{name}/plugins/{plugin}/{rest:path}` matches only if no built-in route matched (registered last).
2. Auth → project resolution → body read.
3. `PluginHost.dispatch_rest(path, project_dir, project_name, body)`; result-or-None dispatching.

### Shutdown

1. `KeyboardInterrupt` or SIGTERM → uvicorn runs lifespan shutdown → in-flight requests complete or time out per uvicorn default (≤30s).
2. WS server shuts down independently.

---

## Acceptance Criteria

- [ ] `api_server.py` is deleted in the same PR that merges this work.
- [ ] All 164 existing routes are reachable at identical paths and methods via the new server; the response-shape crawl test passes.
- [ ] `GET /openapi.json` returns a valid OpenAPI 3.1 document; `openapi-spec-validator` confirms validity in a test.
- [ ] Every route has a stable, descriptive `operationId` matching the convention.
- [ ] All 32 existing chat tool names exist as `operationId`s on their corresponding routes.
- [ ] The full existing test suite (`pytest tests/`) passes with zero changes to test assertions (test fixtures may be refactored to `TestClient(app)` or equivalent).
- [ ] `/render-frame` returns byte-identical JPEGs to a committed legacy fixture for at least 5 `t` values across 2 projects.
- [ ] Range requests on `/files/*` return 206 with correct `Content-Range` and correct bytes for a representative media file.
- [ ] Cookie-based auth and bearer-based auth both succeed end-to-end in tests.
- [ ] Per-project structural lock serializes two concurrent `add-keyframe` calls on the same project; does NOT serialize across projects.
- [ ] Timeline validator warnings still broadcast over WS after structural mutations.
- [ ] Plugin POST routes dispatch to `PluginHost.dispatch_rest`.
- [ ] Validation errors use the legacy `400 BAD_REQUEST` envelope, not FastAPI's default `422`.
- [ ] No performance regression beyond 10% on `/render-frame` and `/files/*` range streaming (measured on the bench fixtures).
- [ ] WS server on port 8891 works unchanged.
- [ ] `pyproject.toml` lists `fastapi`, `uvicorn`, `python-multipart`; no stray imports of `api_server` remain.

---

## Tests

### Base Cases

The core behavior contract: happy path, common bad paths, primary positive and negative assertions. Implementations use `TestClient(app)` (or `httpx.AsyncClient(app=app)`) throughout.

#### Test: get-route-parity (covers R6, R8)

**Given**: A test project with known fixture data (keyframes, transitions, audio tracks).

**When**: For each of a representative set of 20 GET routes (covering every router), the test issues the request against both the legacy server (via a fixture-archived response body) and the new server.

**Then** (assertions):
- **status-match**: new server status code equals legacy status code
- **json-keys-match**: response JSON has identical field names and types
- **json-values-match**: response JSON values match (timestamps within 1s tolerance where server-generated)
- **content-type**: `Content-Type: application/json` for JSON routes

#### Test: post-route-parity (covers R6, R8, R10)

**Given**: Representative POST routes across structural and non-structural mutations.

**When**: POST with a known body; compare DB state before and after.

**Then** (assertions):
- **status-200**: response is 200
- **db-delta-matches**: rows inserted/updated/deleted are identical to legacy's delta for the same input
- **response-shape-matches**: response JSON matches legacy

#### Test: delete-route-parity (covers R6, R8)

**Given**: An existing `track-effect` row.

**When**: `DELETE /api/projects/{name}/track-effects/{id}`.

**Then** (assertions):
- **status-200**: 200 empty body
- **row-removed**: the row is gone from `track_effects`

#### Test: delete-idempotent-parity (covers R6)

**Given**: No row with id `missing-id`.

**When**: `DELETE /api/projects/{name}/track-effects/missing-id`.

**Then** (assertions):
- **status-200**: 200 with empty body (matches M13 idempotent-delete semantics, NOT 404)

#### Test: auth-required-returns-401 (covers R13, R9)

**Given**: No bearer header, no session cookie.

**When**: `GET /api/projects/test/keyframes`.

**Then** (assertions):
- **status-401**: 401
- **envelope-shape**: body is `{"error": "UNAUTHORIZED", "message": "Invalid or expired token"}`
- **content-type**: `application/json`
- **no-session-set**: response does not set a Session cookie

#### Test: bearer-auth-succeeds (covers R13)

**Given**: A valid bearer token for user `u1`.

**When**: `GET /api/config` with `Authorization: Bearer <token>`.

**Then** (assertions):
- **status-200**: 200
- **body-is-config**: body equals `load_config()`

#### Test: cookie-auth-succeeds (covers R13)

**Given**: A valid session cookie set by a prior `/auth/login`.

**When**: `GET /api/projects`.

**Then** (assertions):
- **status-200**: 200
- **body-lists-projects**: body is the expected projects list

#### Test: invalid-json-returns-400 (covers R9, R26)

**Given**: Any POST route expecting JSON body.

**When**: POST with body `"not json at all"` and `Content-Type: application/json`.

**Then** (assertions):
- **status-400**: 400
- **envelope-shape**: body is `{"error": "BAD_REQUEST", "message": "..."}`
- **no-422**: status is NOT 422

#### Test: missing-field-returns-400 (covers R9, R26, R27)

**Given**: `POST /api/projects/create` with body `{}` (missing `name`).

**When**: Request sent.

**Then** (assertions):
- **status-400**: 400
- **message-mentions-name**: `message` contains the word `name`
- **envelope-shape**: `error` is `BAD_REQUEST`

#### Test: options-preflight-204 (covers R11, R49)

**Given**: Any route.

**When**: `OPTIONS` with CORS preflight headers (`Origin`, `Access-Control-Request-Method`).

**Then** (assertions):
- **status-204**: 204 No Content
- **cors-allow-origin**: `Access-Control-Allow-Origin` header set
- **cors-allow-methods**: `Access-Control-Allow-Methods` present

#### Test: file-get-no-range (covers R20, R22)

**Given**: A 100 KB fixture file at `project/assets/test.bin`.

**When**: `GET /api/projects/{name}/files/assets/test.bin` with no `Range` header.

**Then** (assertions):
- **status-200**: 200
- **body-size**: `Content-Length` equals 100 KB
- **body-bytes**: body bytes equal the file bytes
- **accept-ranges**: `Accept-Ranges: bytes` header present

#### Test: file-get-range-206 (covers R21)

**Given**: Same 100 KB fixture.

**When**: `GET` with `Range: bytes=0-999`.

**Then** (assertions):
- **status-206**: 206
- **content-range**: `Content-Range: bytes 0-999/102400`
- **body-length**: body is exactly 1000 bytes
- **body-bytes-match**: body bytes equal file bytes [0:1000]

#### Test: file-head-metadata-only (covers R12)

**When**: `HEAD /api/projects/{name}/files/assets/test.bin`.

**Then** (assertions):
- **status-200**: 200
- **content-length**: `Content-Length: 102400`
- **accept-ranges**: `Accept-Ranges: bytes`
- **empty-body**: response body is empty
- **content-type**: mimetypes-inferred

#### Test: file-traversal-rejected (covers R22)

**When**: `GET /api/projects/{name}/files/../other-project/secret.txt`.

**Then** (assertions):
- **status-404**: 404 `NOT_FOUND`
- **no-leak**: response body does NOT include absolute filesystem paths

#### Test: render-frame-bytes-identical (covers R23)

**Given**: A legacy-captured JPEG fixture for project `P1` at `t=3.5, quality=80`.

**When**: `GET /api/projects/P1/render-frame?t=3.5&quality=80`.

**Then** (assertions):
- **status-200**: 200
- **content-type**: `image/jpeg`
- **bytes-identical**: response body bytes equal the fixture bytes exactly

#### Test: structural-lock-serializes (covers R18)

**Given**: A single project `P1` with a timeline.

**When**: Two `POST /api/projects/P1/add-keyframe` requests issued in parallel using `asyncio.gather`.

**Then** (assertions):
- **both-succeed**: both respond 200
- **no-duplicate-bridge**: the DB contains exactly two new keyframes with distinct ids, no duplicate bridge rows
- **serialization-observed**: handler entry timestamps show the second did not begin until after the first completed (within 5ms)

#### Test: structural-lock-is-per-project (covers R40)

**When**: Concurrent `add-keyframe` on `P1` and `P2`.

**Then** (assertions):
- **both-succeed**: both 200
- **no-serialization-across-projects**: entry timestamps overlap (second started before first completed)

#### Test: timeline-validator-runs-after-mutation (covers R19)

**Given**: A project where a structural mutation will produce a timeline warning.

**When**: The mutation POST runs.

**Then** (assertions):
- **status-200**: 200
- **ws-broadcast**: a WS message `{"type": "timeline_warning", "route": <route>, "warnings": [...]}` was broadcast
- **warnings-logged**: server log contains at least one `Timeline validation` line
- **response-unaffected**: response body is the normal mutation response

#### Test: validator-exception-non-fatal (covers R19)

**Given**: A monkey-patched validator that raises `ValueError("boom")`.

**When**: A structural mutation POST runs.

**Then** (assertions):
- **status-200**: mutation still returns 200
- **no-500**: no 500 response
- **error-logged**: log contains `Validation error: boom`

#### Test: plugin-route-dispatches (covers R25)

**Given**: A plugin `myplugin` registered with a handler that returns `{"ok": true, "data": 42}` for `POST /api/projects/{name}/plugins/myplugin/ping`.

**When**: The POST is issued with an empty body.

**Then** (assertions):
- **status-200**: 200
- **body-matches**: body equals `{"ok": true, "data": 42}`
- **dispatched-with-path**: `PluginHost.dispatch_rest` received the full path as its first arg

#### Test: plugin-error-500 (covers R25)

**When**: Plugin handler raises `RuntimeError("nope")`.

**Then** (assertions):
- **status-500**: 500
- **envelope-code**: `error` is `PLUGIN_ERROR`
- **envelope-message**: `message` includes `nope`

#### Test: plugin-none-returns-404 (covers R25)

**When**: Plugin handler returns None.

**Then** (assertions):
- **status-404**: 404
- **envelope-code**: `error` is `NOT_FOUND`

#### Test: unknown-route-404 (covers R28)

**When**: `GET /api/nope/nope/nope`.

**Then** (assertions):
- **status-404**: 404
- **envelope-code**: `NOT_FOUND`
- **message-contains-path**: `message` mentions the path and method

#### Test: openapi-valid-3-1 (covers R29)

**When**: `GET /openapi.json`.

**Then** (assertions):
- **status-200**: 200
- **openapi-version**: `openapi` field is `"3.1.0"` or higher
- **validator-passes**: `openapi_spec_validator.validate(spec)` raises no error

#### Test: every-route-has-operation-id (covers R7, R30)

**Given**: The emitted OpenAPI doc.

**When**: Iterate paths and methods.

**Then** (assertions):
- **all-have-id**: every operation has `operationId`
- **all-ids-unique**: no two operations share an operationId
- **ids-are-snake-case**: every id matches `^[a-z][a-z0-9_]*$`

#### Test: chat-tool-operation-ids-match (covers R7, R47)

**Given**: The current 32 chat tool names from `chat.py::TOOLS`.

**When**: Inspect OpenAPI spec.

**Then** (assertions):
- **all-32-present**: every chat tool name appears as an `operationId` on at least one route
- **no-typos**: spelling matches exactly

#### Test: legacy-test-suite-green (covers R6, R8)

**When**: `pytest tests/` runs against the new server (test fixtures updated to use `TestClient`).

**Then** (assertions):
- **zero-failures**: 0 test failures
- **zero-errors**: 0 test errors
- **count-unchanged**: test count is unchanged from the pre-migration baseline

#### Test: response-shape-parity-crawl (covers R8, R29)

**Given**: A crawl fixture listing 30 (route, request-body-or-none) pairs covering every router.

**When**: Each is issued against both servers (legacy fixture captured pre-migration, new live).

**Then** (assertions):
- **shapes-equivalent**: JSON shapes match (same keys, same types, values equal or within documented tolerances)
- **no-extra-keys**: new response has no extra keys vs legacy
- **no-missing-keys**: new response has no missing keys vs legacy

#### Test: cli-starts-uvicorn (covers R3, R33)

**When**: `scenecraft serve --host 127.0.0.1 --port 0` (or the current equivalent).

**Then** (assertions):
- **process-binds-port**: a port is bound
- **process-responds**: `GET /openapi.json` on the bound port returns 200

#### Test: legacy-server-deleted (covers R1, R34)

**When**: Check the working tree.

**Then** (assertions):
- **file-absent**: `src/scenecraft/api_server.py` does not exist
- **no-imports**: `git grep "from scenecraft.api_server"` returns zero hits in `src/` and `tests/`

### Edge Cases

Boundaries, unusual inputs, concurrency, idempotency, ordering, time-dependent behavior, resource exhaustion.

#### Test: file-get-range-416 (covers R21)

**Given**: Fixture file is 100 KB.

**When**: `GET` with `Range: bytes=200000-300000`.

**Then** (assertions):
- **status-416**: 416 Range Not Satisfiable
- **content-range-size**: response sets `Content-Range: bytes */102400`

#### Test: file-get-suffix-range-416 (covers R21)

**When**: `GET` with `Range: bytes=-100`.

**Then** (assertions):
- **status-416**: 416 (legacy does not support suffix ranges; preserve that)

#### Test: options-always-204 (covers R11, R33)

**When**: `OPTIONS /api/does/not/exist`.

**Then** (assertions):
- **status-204**: 204
- **cors-headers-present**: CORS headers set

#### Test: lock-released-on-exception (covers R18, R45)

**Given**: A handler monkey-patched to raise.

**When**: Two sequential structural POSTs on `P1`, first raises.

**Then** (assertions):
- **first-status-500**: first request returns 500
- **second-status-200**: second request succeeds (lock was released on exception)

#### Test: validator-exception-lock-released (covers R18, R19)

**Given**: Validator monkey-patched to raise.

**When**: Structural POST, then immediate follow-up structural POST.

**Then** (assertions):
- **first-200**: 200
- **second-200-not-blocked**: second completes within 100 ms (lock was released)

#### Test: extra-fields-ignored (covers R10, R43)

**Given**: POST body `{"name": "foo", "unknown_field": 42}` to a route expecting only `name`.

**When**: Request sent.

**Then** (assertions):
- **status-200**: 200
- **unknown-field-ignored**: no error; DB reflects only `name=foo`

#### Test: builtin-beats-plugin-catchall (covers R25, R56)

**Given**: A plugin registers `POST /api/projects/{name}/plugins/x/add-keyframe` (shadowing the built-in structural route — implausible but testable).

**When**: Request issued.

**Then** (assertions):
- **built-in-wins**: Built-in `add-keyframe` handler runs (registration order: built-ins first, plugin catch-all last)
- **regression-guard**: test documents this ordering

#### Test: deprecated-noops-preserved (covers R57)

**When**: `POST /api/projects/{name}/version/commit`.

**Then** (assertions):
- **status-200**: 200
- **body-matches-legacy**: response body is identical to legacy (noop shape)

#### Test: no-body-post-works (covers R58)

**When**: `POST /api/projects/{name}/undo` with no body.

**Then** (assertions):
- **status-200**: 200
- **undo-applied**: DB reflects one undo unit popped

#### Test: large-upload-streams (covers R52)

**Given**: A 200 MB test file and `POST /api/projects/{name}/pool/upload`.

**When**: Upload issued.

**Then** (assertions):
- **status-200**: 200
- **peak-rss-bounded**: server process peak RSS during the upload stays under (baseline + 50 MB)
- **file-uploaded**: the target file exists on disk with correct size

#### Test: render-frame-perf-no-regression (covers R23)

**Given**: Legacy p50 of 65 ms for `/render-frame?t=3.5` over 100 calls (captured pre-migration, stored as `tests/fixtures/perf_baseline.json`).

**When**: 100 calls run against the new server.

**Then** (assertions):
- **p50-within-10pct**: new p50 ≤ 1.10 × legacy p50
- **p99-within-25pct**: new p99 ≤ 1.25 × legacy p99

#### Test: files-range-perf-no-regression (covers R21)

**Given**: 100 MB media file, legacy baseline for 1 MB range fetches.

**When**: 100 range fetches on the new server.

**Then** (assertions):
- **throughput-within-10pct**: new MB/s ≥ 0.90 × legacy MB/s

#### Test: cors-origin-matches-legacy (covers R49, R50)

**Given**: Legacy allow-origin list is `*` or a specific list (whichever the current `_cors_headers` sets).

**When**: Inspect `Access-Control-Allow-Origin` on a normal response.

**Then** (assertions):
- **exact-match**: value matches legacy output for the same request

#### Test: test-client-replaces-http-server-fixture (covers R2, R28)

**Given**: The pre-migration fixture spins up `HTTPServer(('', 0), make_handler(...))` on a background thread.

**When**: Migrate the fixture to `TestClient(app)`.

**Then** (assertions):
- **same-assertions-pass**: every test that used the old fixture passes
- **no-thread-leaks**: no orphaned server threads after the test module exits

#### Test: openapi-snapshot-diff-flagged (covers R32)

**Given**: Committed snapshot at `tests/fixtures/openapi.snapshot.json`.

**When**: Remove an operation from the app, regenerate spec.

**Then** (assertions):
- **test-fails**: snapshot test fails
- **diff-highlights-removal**: failure message names the missing operation

#### Test: graceful-shutdown-during-stream (covers R42)

**Given**: A client streaming a 1 GB `/download-preview` response.

**When**: Server receives SIGTERM mid-stream.

**Then** (assertions):
- **stream-terminates-cleanly**: client sees EOF within 30 s
- **no-traceback-at-error**: server log has no ERROR-level traceback for the aborted stream

#### Test: chat-exec-paths-unaffected (covers R46)

**Given**: A chat session invoking `add_audio_track` via `_exec_add_audio_track`.

**When**: Tool runs.

**Then** (assertions):
- **tool-succeeds**: the `_exec_*` path returns its expected result
- **no-http-call**: no network socket was opened by this path (verified via monkey-patched `httpx`/`urllib`)

#### Test: swagger-ui-renders (covers R31)

**When**: `GET /docs`.

**Then** (assertions):
- **status-200**: 200
- **content-type-html**: `text/html`
- **contains-swagger**: response body contains `swagger-ui` JS bundle reference

#### Test: ws-server-independent (covers R30)

**When**: Uvicorn running on 8890; WS server running on 8891.

**Then** (assertions):
- **ws-connect-succeeds**: a client connects to `ws://localhost:8891`
- **ws-broadcast-delivered**: a broadcast from the WS server reaches the client
- **rest-uninterrupted**: simultaneous `GET /openapi.json` responds normally

#### Test: unhandled-exception-500-envelope (covers R48)

**Given**: A monkey-patched route that raises `RuntimeError("kaboom")`.

**When**: The route is called.

**Then** (assertions):
- **status-500**: 500
- **envelope-shape**: `{"error": "INTERNAL_ERROR", ...}`
- **traceback-in-log**: server log contains the traceback (not leaked in response body)

#### Test: invalid-path-encoding-400 (covers R41)

**When**: `GET /api/projects/{name}/files/%FF%FE%FD` (invalid UTF-8 after decoding).

**Then** (assertions):
- **status-400**: 400 BAD_REQUEST
- **no-leak**: response does NOT include a raw decoded path

#### Test: validation-envelope-legacy-shape (covers R26)

**Given**: A POST with a body that fails Pydantic validation (wrong type on a required field).

**When**: Request sent.

**Then** (assertions):
- **status-400**: 400 (NOT 422)
- **envelope-code**: `BAD_REQUEST`
- **no-nested-errors-list**: response body does NOT include FastAPI's default `{"detail": [...]}` shape

#### Test: oauth-callback-bad-state (covers R17)

**When**: `GET /oauth/callback?code=good&state=tampered`.

**Then** (assertions):
- **status-400**: 400
- **envelope-code**: `BAD_REQUEST`
- **no-token-stored**: no OAuth token was persisted

#### Test: auth-login-sets-cookie-and-redirects (covers R15)

**When**: `GET /auth/login?code=<valid>`.

**Then** (assertions):
- **status-302-or-200**: matches legacy exactly
- **set-cookie-httponly**: `Set-Cookie` header has `HttpOnly`, `SameSite=Lax`, `Secure` (in prod)
- **redirect-location**: Location header matches legacy target

#### Test: auth-logout-clears-cookie (covers R16)

**When**: `POST /auth/logout`.

**Then** (assertions):
- **status-200**: 200
- **set-cookie-max-age-0**: `Set-Cookie` has `Max-Age=0`
- **body-ok**: body is `{"ok": true}`

#### Test: oauth-callback-success (covers R17)

**When**: Callback with valid code + state.

**Then** (assertions):
- **status-302**: redirects to app
- **token-stored**: token row inserted in the OAuth store

#### Test: multipart-upload-parity (covers R33, R51)

**Given**: `python-multipart` installed.

**When**: Multipart upload of a small file to `/pool/upload`.

**Then** (assertions):
- **status-200**: 200
- **pool-row-added**: a new pool segment row exists with the uploaded file
- **bytes-match**: uploaded file bytes equal source

#### Test: operation-id-set-for-every-route (covers R7, R30)

**Given**: The running app.

**When**: Inspect `app.routes`.

**Then** (assertions):
- **every-route-has-operation-id**: no route has `operation_id == None`
- **matches-naming-convention**: all operation IDs are snake_case

#### Test: cli-help-unchanged (covers R3, R44)

**When**: `scenecraft --help`.

**Then** (assertions):
- **flags-unchanged**: the list of flags and their help text matches pre-migration output

---

## UI-Structure Test Strategy

No UI changes in this spec. The scenecraft frontend continues to call the same routes with the same shapes; no code changes are required on the frontend side of the cutover.

---

## Non-Goals

Summarized from Out-of-Scope above; restated for proofing clarity:

- Tool schema codegen (see `agent/specs/local.openapi-tool-codegen.md`)
- WebSocket server migration
- New endpoints, new auth providers, new OAuth flows
- Changing HTTP auth model (still bearer + cookie + OAuth callback)
- Rewriting business logic in `db.py`, `chat.py`, etc.
- A feature-flag toggle between legacy and FastAPI (hard cut)
- Performance tuning beyond parity
- MCP server integration
- Frontend code changes

---

## Open Questions

1. **OQ-1** — Should `HEAD` be supported on `/render-frame` for browser preload hints? Legacy does not implement this; FastAPI with `methods=["GET", "HEAD"]` would auto-generate it but would invoke the encoder once per HEAD, which is wasteful. Default: do not add HEAD on `/render-frame`. **Captured as Behavior Table row 59.**
2. **OQ-2** — For **new** endpoints added after this migration, should the default validation envelope be the legacy 400 shape (this spec's R26) or FastAPI's native 422 shape? Preserving legacy forever has a cost — future endpoints might benefit from the richer 422 detail. Default assumed for now: keep legacy envelope everywhere. **Captured as Behavior Table row 60.**
3. **OQ-3** — Is there an existing `api_server`-shaped test fixture that lives outside `tests/` (e.g., in `scripts/` or a developer smoke test) that also needs to be migrated? Audit during Phase 0.
4. **OQ-4** — Does any current route set response headers beyond CORS + Content-Type + Content-Length + Accept-Ranges + Content-Range + Set-Cookie? If yes, enumerate during the router split and preserve. Partial audit done; full audit deferred to implementation.
5. **OQ-5** — Uvicorn vs hypercorn vs daphne? Default: `uvicorn[standard]` (matches most-common FastAPI deployment, has uvloop + httptools). Can revisit if a perf gap appears.

---

## Key Design Decisions

Captured from chat on 2026-04-24:

- **Hard cut**, not feature-flagged. User accepts migration risk given the 897-test suite, of which only 11 files touch HTTP directly.
- **Two specs**, not one. This spec is the migration; `local.openapi-tool-codegen.md` is the tool-codegen layer built on top.
- **Chat tool execution path is NOT scoped to this spec.** The tool-codegen spec owns that decision (see its OQ-1). This spec's R46 guarantees only that the existing `_exec_*` paths keep working; it does not constrain how chat tools dispatch after the tool-codegen ships.
- **WebSocket server stays independent** on port 8891. Merging WS into FastAPI is a separate decision and out of scope here.
- **Uvicorn** is the default ASGI server. `uvicorn[standard]` gives uvloop + httptools without extra deps.
- **Error envelope is preserved** even for validation errors. Accepting FastAPI's default 422 shape would break existing clients.
- **`operationId` convention is snake_case imperative verb phrases**; the 32 existing chat tool names are adopted verbatim as operation IDs to make the tool-codegen spec's job mechanical.

---

## Related Artifacts

- `src/scenecraft/api_server.py` — the module being replaced (10,320 LOC, 164 routes)
- `src/scenecraft/ws_server.py` — WebSocket server (unchanged; runs on port 8891)
- `src/scenecraft/chat.py` — chat tool registry and `_exec_*` paths (unaffected by this spec; consumed by the tool-codegen spec)
- `src/scenecraft/plugin_host.py` — `PluginHost.dispatch_rest` (unchanged; called by catch-all route)
- `tests/` — existing 897-test suite, primary verification surface
- `agent/specs/local.openapi-tool-codegen.md` — the follow-on spec that consumes this spec's `/openapi.json`
- `agent/progress.yaml` — project tracker; milestone + task entries added when this spec is approved
