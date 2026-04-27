# Spec: engine-rest-api-dispatcher

> **Agent Directive**: This spec is the black-box contract for the engine REST dispatcher. It is the acceptance-test suite for the forthcoming FastAPI migration. Every row in the Behavior Table MUST remain green after the rewrite. Do not change observable behavior to match FastAPI defaults — override FastAPI to match this spec.

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Active — refactor-regression contract

---

## Purpose

Define, at the HTTP level, the observable contract of the engine REST API dispatcher (`api_server.py`, currently a hand-wired `BaseHTTPRequestHandler`). The dispatcher owns: URL routing, auth gate, request-body parsing, response serialization, error shape, CORS, structural locking, timeline post-validation, plugin REST fallback, and file serving (range/ETag/304). Individual handler logic is out of scope and specced elsewhere (bounce, analysis, generation, etc.).

This spec is written so that the FastAPI rewrite can be driven TDD-style: each Behavior Table row maps to a Given/When/Then test that asserts on HTTP status, response body shape, response headers, and observable side effects (WS broadcast, DB row count, file on disk, Set-Cookie presence). Implementation-internal concerns (which router class owns a route, which Pydantic model validates a body) are explicitly NOT constrained — only observable HTTP behavior is.

## Source

- `--from-draft` (agent brief in chat — regression-test spec for REST dispatcher, sourced from audit-2 §1A + §3)
- Code: `src/scenecraft/api_server.py` (10,642 LOC), `src/scenecraft/auth_middleware.py`, `src/scenecraft/vcs/auth.py`, `src/scenecraft/plugin_host.py:456–481`
- Audit: `agent/reports/audit-2-architectural-deep-dive.md` §1A (13 dispatcher units) and §3 (leaks #4, #5, #7, #11, #12, #13, #24)

## Scope

**In scope**:
- URL routing: path-method dispatch, 404 on no-route, per-method method-not-allowed via HTTP status
- Auth gate: bearer + cookie extraction, precedence, JWT validation, sliding cookie refresh, exempt paths
- Paid-plugin gate (`@require_paid_plugin_auth`): currently dead code; document status as OQ
- CORS: Origin echo, credentials=true, no allowlist (documented as XSRF exposure, flagged OQ)
- Error shape: `{error: <message>, code: <string>}`, status code, error-code-string registry
- Structural locking: per-project mutex on 11 routes; post-mutation timeline-validation hook
- Plugin REST dispatch fallback: `/api/projects/:name/plugins/:plugin_id/*` regex routing, `path_groups=` kwarg propagation
- File serving: Range (206 partial), ETag (304), If-Modified-Since (304), 65KB chunked read, path-traversal guard (`startswith` on resolved path — symlink-bypassable, flagged OQ)
- Method-level dispatchers: `do_GET`, `do_POST`, `do_DELETE`, `do_HEAD`, `do_OPTIONS`
- Full catalog of every current endpoint, one Behavior Table row per

**Out of scope**:
- Individual handler business logic (keyframe mutation, candidate generation, bounce, analysis)
- WebSocket upgrade (`/ws/*` is a separate process, `ws_server.py` — only mentioned)
- DAL / DB schema (separate specs)
- Render pipeline, generation providers, spend ledger
- Frontend consumption (mix-render-upload body is spec'd at HTTP shape only; audio rendering spec'd elsewhere)

## Requirements

### Routing

- **R1** — Dispatcher routes each request by `(method, path)`. Unknown `(method, path)` returns `404 {error: "No route: <METHOD> <path>", code: "NOT_FOUND"}`.
- **R2** — All `/api/**` and `/auth/**` and `/oauth/**` paths use the dispatcher; `/ws/**` upgrades to WebSocket and is NOT served by this dispatcher.
- **R3** — Path parameters use regex named or numbered groups (`[^/]+` for names/ids, `(.+)` for file paths). Project names MUST NOT contain `/`.
- **R4** — Query strings are parsed with `urllib.parse` semantics; URL-encoded path segments (`%20`, etc.) are `unquote`d before matching.
- **R5** — After all built-in routes miss, dispatcher tries `PluginHost.dispatch_rest(method, path, project_dir, project_name, body_or_query)` for paths matching `^/api/projects/([^/]+)/plugins/[^/]+/`. If plugin returns non-None, it is JSON-serialized with status 200. If plugin raises, dispatcher returns `500 {code: "PLUGIN_ERROR", error: <exception str>}`. If plugin returns None AND no built-in matched, 404.

### Authentication

- **R6** — When `.scenecraft` root exists (auth enabled), every request except exempt paths MUST carry a valid JWT.
- **R7** — Exempt paths (no auth): `/auth/login`, `/auth/logout`, `/oauth/callback` (the OAuth provider-side redirect lands unauthenticated). `/api/_internal/broadcast` is **not** exempt but is typically bound to localhost.
- **R8** — Token extraction order: `Authorization: Bearer <token>` first; if absent, `Cookie: scenecraft_jwt=<token>`. Bearer takes precedence.
- **R9** — Missing token → `401 {code: "UNAUTHORIZED", error: "Not authenticated"}`.
- **R10** — Invalid/expired token → `401 {code: "UNAUTHORIZED", error: "Invalid or expired token"}`.
- **R11** — On valid cookie-based auth, dispatcher regenerates a fresh JWT and sets `Set-Cookie: scenecraft_jwt=<new>; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400` on the response (sliding refresh). Bearer-authed requests do NOT refresh.
- **R12** — JWT payload `sub` is the authenticated username; exposed on the handler as `_authenticated_user` for downstream use (OAuth scope, paid-plugin gate).
- **R13** — When auth is disabled (`--no-auth` or `.scenecraft` root not found), `_authenticate()` returns True unconditionally and never emits 401.

### Paid-plugin gate (currently dead code)

- **R14** — `@require_paid_plugin_auth(sc_root)` decorator is defined in `auth_middleware.py` and performs a double gate: JWT (cookie or bearer) + `X-Scenecraft-API-Key` header (PBKDF2-hashed lookup in `api_keys` table) + `must_change_password` check + org resolution (`X-Scenecraft-Org` header → session → single-org fallback). Error codes: `UNAUTHORIZED`, `PASSWORD_CHANGE_REQUIRED`, `ORG_NOT_FOUND`, `AMBIGUOUS_ORG`.
- **R15** — As of the audit date, the decorator is applied to **zero** endpoints. Dispatcher treats it as dead code. FastAPI rewrite MUST carry the decorator forward (or its equivalent dependency) so routes can opt in, but MUST NOT silently apply it to any endpoint without an explicit OQ resolution. See [OQ-1](#open-questions).

### CORS

- **R16** — On every response (including 401, 404, 500), the dispatcher emits:
  - `Access-Control-Allow-Origin: <echoed Origin header, else *>`
  - `Access-Control-Allow-Credentials: true` — **only when** Origin is present
  - `Vary: Origin` — only when Origin is echoed
  - `Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS`
  - `Access-Control-Allow-Headers: Content-Type, Authorization, X-Scenecraft-Branch`
- **R17** — `OPTIONS` (preflight) returns 204 with CORS headers and empty body for **any** path, without consulting auth or routing.
- **R18** — There is NO Origin allowlist. Any Origin is echoed. With credentials=true this is an XSRF exposure: a malicious origin can cause the user's browser to ship the `scenecraft_jwt` cookie cross-site. See [OQ-2](#open-questions).

### Request body parsing

- **R19** — POST/DELETE/PATCH/PUT handlers that need a body call `_read_json_body()`:
  - Reads `Content-Length` bytes from `rfile`
  - `Content-Length: 0` → `400 {code: "BAD_REQUEST", error: "Empty body"}`
  - Non-JSON body → `400 {code: "BAD_REQUEST", error: "Invalid JSON: <parse detail>"}`
  - Returns parsed `dict` on success
- **R20** — Plugin POST dispatch uses `_read_json_body() or {}` — an empty body is tolerated as `{}` for plugin endpoints only (built-in handlers call `_read_json_body()` directly and get 400 on empty).
- **R21** — Multipart uploads (`pool/upload`, `bounce-upload`, `mix-render-upload`, `bench/upload`, `pool/import`) read the raw stream themselves and do NOT go through `_read_json_body`.

### Response serialization

- **R22** — Successful JSON responses: `Content-Type: application/json`, `Content-Length` set, status default 200, body is `json.dumps(obj).encode()`.
- **R23** — If `_refreshed_cookie` was set during auth, the response also carries `Set-Cookie: <refreshed>`.
- **R24** — Client disconnects mid-send (BrokenPipeError / ConnectionResetError) are swallowed — no exception propagates.

### Error shape

- **R25** — All error responses use the shape `{"error": <human string>, "code": <SCREAMING_SNAKE string>}`. Status code is set on the HTTP response; `code` is the machine-readable token.
- **R26** — Error `code` values observed in source include: `NOT_FOUND`, `BAD_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN`, `INVALID_CODE`, `UNKNOWN_SERVICE`, `AUTH_DISABLED`, `INTERNAL_ERROR`, `PLUGIN_ERROR`, `PASSWORD_CHANGE_REQUIRED`, `ORG_NOT_FOUND`, `AMBIGUOUS_ORG` plus ~30 additional ad-hoc strings at individual call sites (audit leak #24). FastAPI migration MUST preserve the codes emitted by every current call site. The catalog is authoritative.
- **R27** — FastAPI's default error body shape is `{"detail": ...}`. The rewrite MUST override the default exception handler to emit `{error, code}` instead. See Migration Contract.

### Structural locking

- **R28** — Per-project `threading.Lock` is acquired before invoking the handler for POST routes whose **last path segment** is one of: `add-keyframe`, `duplicate-keyframe`, `delete-keyframe`, `batch-delete-keyframes`, `restore-keyframe`, `delete-transition`, `restore-transition`, `split-transition`, `insert-pool-item`, `paste-group`, `checkpoint`. Exactly 11 routes.
- **R29** — Locks are per-project-name, lazily created and memoized in a dispatcher-scoped dict.
- **R30** — All other POST, GET, DELETE routes run with NO lock. Concurrent writes to the same project from non-locked routes race freely. (Audit leak #7.)
- **R31** — After a locked route handler completes (success OR exception), dispatcher calls `validate_timeline(project_dir)`. Any warnings are:
  - Logged via `_log(...)` with the `⚠ Timeline validation` prefix
  - Broadcast on WebSocket `job_manager` as `{type: "timeline_warning", route, warnings}`
  - Do NOT block or modify the HTTP response
- **R32** — Lock is released in a `finally` — handler exceptions do not deadlock the project.

### File serving (`GET /api/projects/:name/files/*`)

- **R33** — Resolve `full = (work_dir / project / file_path).resolve()`; reject with `403 FORBIDDEN "Path traversal denied"` if `str(full)` does not `startswith(str(work_dir.resolve()))`.
- **R34** — Note: the traversal guard does NOT re-resolve to catch symlinks pointing outside the work dir — audit leak #11 marks this as bypassable. Preserved as-is for refactor parity. See [OQ-3](#open-questions).
- **R35** — Missing file → `404 NOT_FOUND "File not found: <path>"`.
- **R36** — ETag is `"<size_hex>-<mtime_hex>"`. `If-None-Match` match → `304` with ETag and CORS headers, empty body.
- **R37** — `If-Modified-Since` older than or equal to mtime → `304`. Malformed date → treated as absent, full response emitted.
- **R38** — `Range: bytes=<start>-<end>` → `206 Partial Content` with `Content-Range: bytes <start>-<end>/<size>`, `Accept-Ranges: bytes`, `Cache-Control: public, max-age=3600, immutable`, `ETag`, `Last-Modified`. Response body is chunked 65536 bytes at a time via `f.read(65536)`. `end` defaults to `size-1`.
- **R39** — `Range: bytes=0-` (common browser preload) is served via chunked loop to avoid OOM.
- **R40** — Full-file response: `200`, `Content-Type` from `mimetypes.guess_type` (fallback `application/octet-stream`), `Accept-Ranges: bytes`, cache headers, chunked 65536 write.
- **R41** — `HEAD /api/projects/:name/files/*` returns `200` with `Content-Type`, `Content-Length`, `Accept-Ranges: bytes`, CORS, empty body. Missing file → `404` with empty body (NOT the `{error,code}` shape).
- **R42** — `HEAD` on any other path → `405` empty body.

### Plugin REST dispatch

- **R43** — Plugins register routes via `PluginHost.register_rest_endpoint(method, pattern, handler)`. Patterns auto-prefix `^/api/projects/(?P<project>[^/]+)/plugins/<plugin_id>`.
- **R44** — `PluginHost.dispatch_rest(method, path, *args, **kwargs)` iterates `_rest_routes_by_method[method.upper()]` in insertion order, returns the first regex match's handler result.
- **R45** — Named regex groups in the registered pattern are extracted and passed as `path_groups=<dict>` kwarg. Patterns with no named groups do NOT receive `path_groups` (back-compat with pre-task-130 handlers whose signatures lack `**kwargs`).
- **R46** — Dispatcher calls `PluginHost.dispatch_rest("GET", path, project_dir, project_name, query)` for GET and `("POST", path, project_dir, project_name, body)` for POST. DELETE/PATCH/PUT plugin routes are NOT dispatched by the current dispatcher — only GET and POST reach the plugin fallback. See [OQ-4](#open-questions).
- **R47** — Plugin handler returning `None` means "no match, keep trying"; returning any JSON-serializable value means success, dispatcher wraps with `_json_response`.
- **R48** — Plugin handler exceptions become `500 PLUGIN_ERROR <str(e)>`.

### Timeline validation

- **R49** — Only invoked on the 11 locked routes (R28). Not on non-locked POSTs, not on GETs, not on DELETEs.
- **R50** — First 10 warnings logged; all warnings broadcast. Response is unaffected.

---

## Interfaces / Data Shapes

### Error body
```json
{"error": "human-readable message", "code": "SCREAMING_SNAKE_CODE"}
```

### Successful JSON body
Per-endpoint; dispatcher merely serializes whatever the handler returned via `_json_response(obj)`.

### Cookie
```
scenecraft_jwt=<jwt>; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400
```
(No `Secure` flag unless the deployment opts in; HTTPS-terminating proxy is assumed in prod.)

### CORS response headers
```
Access-Control-Allow-Origin: <Origin or *>
Access-Control-Allow-Credentials: true     # only if Origin present
Vary: Origin                                # only if Origin present
Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS
Access-Control-Allow-Headers: Content-Type, Authorization, X-Scenecraft-Branch
```

### Range response headers (206)
```
Content-Type: <mime>
Content-Length: <bytes>
Content-Range: bytes <start>-<end>/<size>
Accept-Ranges: bytes
ETag: "<size_hex>-<mtime_hex>"
Last-Modified: <RFC 2822>
Cache-Control: public, max-age=3600, immutable
```

---

## Full Endpoint Catalog

Notation:
- **Auth**: `yes` (JWT required), `exempt` (exempt path), `n/a` (no-auth mode serves all).
- **Lock**: `Y` = structural lock + timeline validation; `-` = none.
- Body / Response shape columns: `JSON` = JSON object, `MP` = multipart, `-` = none, `<shape>` = specific shape; actual field sets deliberately left at the handler-spec level.

### GET

| Method | Path | Auth | Body | Response | Status codes | Lock | Notes |
|---|---|---|---|---|---|---|---|
| GET | `/auth/login?code=…&redirect_uri=…` | exempt | — | 303 redirect + Set-Cookie | 303, 400, 401, 501 | — | Consumes one-time login code, sets cookie, redirects |
| GET | `/oauth/callback?code=…&state=…` | exempt | — | HTML | 200 | — | Renders popup-close HTML; no JSON |
| GET | `/api/oauth/:service/authorize` | yes | — | `{url,state}` | 200, 404 | — | `UNKNOWN_SERVICE` for bad service |
| GET | `/api/oauth/:service/status` | yes | — | `{connected, expires_at?, has_refresh_token?, created_at?, updated_at?}` | 200, 404 | — | |
| GET | `/api/config` | yes | — | config dict | 200 | — | |
| GET | `/api/projects` | yes | — | `{projects:[…]}` | 200 | — | |
| GET | `/api/browse?path=` | yes | — | `{path, entries:[…]}` | 200, 403, 404 | — | 403 on traversal |
| GET | `/api/render-cache/stats` | yes | — | stats dict | 200 | — | |
| GET | `/api/projects/:name/keyframes` | yes | — | `{keyframes:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/beats` | yes | — | `{beats:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/ls?path=` | yes | — | `{entries:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/bin` | yes | — | bin contents | 200, 404 | — | |
| GET | `/api/projects/:name/watched-folders` | yes | — | `{folders:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/narrative` | yes | — | `{sections:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/workspace-views` | yes | — | `{views:{…}}` | 200, 404 | — | |
| GET | `/api/projects/:name/workspace-views/:view` | yes | — | `{layout}` | 200, 404 | — | Missing view → 404 |
| GET | `/api/projects/:name/chat?limit=50` | yes | — | `{messages:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/checkpoints` | yes | — | `{checkpoints:[…], active}` | 200, 404 | — | |
| GET | `/api/projects/:name/undo-history` | yes | — | history list | 200, 404 | — | |
| GET | `/api/projects/:name/settings` | yes | — | settings dict | 200, 404 | — | |
| GET | `/api/projects/:name/ingredients` | yes | — | `{ingredients:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/bench` | yes | — | bench dict | 200, 404 | — | |
| GET | `/api/projects/:name/section-settings?section=` | yes | — | settings dict | 200, 404 | — | |
| GET | `/api/projects/:name/audio-intelligence` | yes | — | `{}` (stub) | 200, 404 | — | |
| GET | `/api/projects/:name/render-state` | yes | — | `{buckets:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/render-frame?t=&quality=` | yes | — | image bytes | 200, 400, 404 | — | |
| GET | `/api/projects/:name/descriptions` | yes | — | `{descriptions:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/staging/:stagingId` | yes | — | staging dict | 200, 404 | — | |
| GET | `/api/projects/:name/download-preview?start=&end=` | yes | — | video bytes | 200, 400, 404 | — | |
| GET | `/api/projects/:name/tracks` | yes | — | `{tracks:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/audio-tracks` | yes | — | `{tracks:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/track-effects?track_id=` | yes | — | `{effects:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/master-bus-effects` | yes | — | `{effects:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/send-buses` | yes | — | `{buses:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/audio-clips` | yes | — | `{clips:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/audio-clips/:id/peaks?resolution=` | yes | — | `{peaks:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/unselected-candidates` | yes | — | `{candidates:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/video-candidates?limit=` | yes | — | `{candidates:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/markers` | yes | — | `{markers:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/prompt-roster` | yes | — | `{prompts:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/pool` | yes | — | `{segments:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/pool/tags` | yes | — | `{tags:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/pool/gc-preview` | yes | — | `{preview:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/branches` | yes | — | `{branches:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/version/history` | yes | — | deprecated | 200, 404 | — | Always empty now |
| GET | `/api/projects/:name/version/diff` | yes | — | deprecated | 200, 404 | — | Always empty now |
| GET | `/api/projects/:name/effects` | yes | — | `{effects:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/thumb/*` | yes | — | image bytes | 200, 404 | — | |
| GET | `/api/projects/:name/thumbnail/*` | yes | — | JPEG bytes | 200, 404 | — | First-frame video |
| GET | `/api/projects/:name/transitions/:tr_id/filmstrip?t=&height=` | yes | — | image bytes | 200, 404 | — | |
| GET | `/api/projects/:name/files/*` | yes | — | file bytes | 200, 206, 304, 403, 404 | — | Range/ETag/IMS |
| GET | `/api/projects/:name/pool/:seg_id/peaks?resolution=` | yes | — | `{peaks:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/audio-isolations?entityType=&entityId=` | yes | — | `{isolations:[…]}` | 200, 404 | — | |
| GET | `/api/projects/:name/bounces/:id.wav` | yes | — | WAV bytes | 200, 404 | — | |
| GET | `/api/projects/:name/plugins/:plugin/*` | yes | — | plugin JSON | 200, 404, 500 | — | Plugin REST fallback |

### POST

| Method | Path | Auth | Body | Response | Status codes | Lock | Notes |
|---|---|---|---|---|---|---|---|
| POST | `/auth/logout` | exempt | — | `{ok:true}` + clear cookie | 200 | — | |
| POST | `/api/oauth/:service/disconnect` | yes | — | `{disconnected}` | 200, 404 | — | |
| POST | `/api/_internal/broadcast` | yes¹ | `{type,…}` | `{ok:true}` | 200, 400, 500 | — | ¹IPC from local processes |
| POST | `/api/config` | yes | JSON | `{ok}` | 200, 400 | — | |
| POST | `/api/projects/create` | yes | JSON | created proj | 200, 400 | — | |
| POST | `/api/projects/:name/select-keyframes` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/select-slot-keyframes` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/select-transitions` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-timestamp` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-transition-trim` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/clip-trim-edge` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/move-transitions` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-prompt` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/add-keyframe` | yes | JSON | `{keyframe}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/duplicate-keyframe` | yes | JSON | `{keyframe}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/paste-group` | yes | JSON | `{ok}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/delete-keyframe` | yes | JSON | `{ok}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/batch-delete-keyframes` | yes | JSON | `{ok}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/restore-keyframe` | yes | JSON | `{ok}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/batch-set-base-image` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/set-base-image` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/delete-transition` | yes | JSON | `{ok}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/restore-transition` | yes | JSON | `{ok}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/unlink-keyframe` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-transition-action` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-transition-remap` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/generate-transition-action` | yes | JSON | `{action}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/enhance-transition-action` | yes | JSON | `{action}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/pool/add` | yes | JSON | `{segment}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/pool/import` | yes | JSON | `{segment}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/pool/upload` | yes | MP | `{segment}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/mix-render-upload` | yes | MP | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/bounce-upload` | yes | MP | `{bounceId}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/pool/rename` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/pool/tag` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/pool/untag` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/pool/gc` | yes | JSON | `{deleted:[…]}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/assign-pool-video` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/undo` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/redo` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/workspace-views/:view` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/workspace-views/:view/delete` | yes | — | `{ok}` | 200, 404 | — | |
| POST | `/api/projects/:name/checkpoint` | yes | JSON | `{checkpoint}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/checkpoint/restore` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/checkpoint/delete` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/bench/capture` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/bench/upload` | yes | MP | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/bench/add` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/bench/remove` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/tracks/add` | yes | JSON | `{track}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/tracks/update` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/tracks/delete` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/tracks/reorder` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-tracks/add` | yes | JSON | `{track}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-tracks/update` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-tracks/delete` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-tracks/reorder` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/transitions/:id/link-audio` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-clips/add` | yes | JSON | `{clip}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-clips/add-from-pool` | yes | JSON | `{clip}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-clips/update` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-clips/delete` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-clips/batch-ops` | yes | JSON | `{results:[…]}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/audio-clips/align-detect` | yes | JSON | `{offset}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-rules` | yes | JSON | `{}` (stub) | 200, 404 | — | |
| POST | `/api/projects/:name/reapply-rules` | yes | JSON | `{}` (stub) | 200, 404 | — | |
| POST | `/api/projects/:name/generate-keyframe-variations` | yes | JSON | `{jobId,keyframeId}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/escalate-keyframe` | yes | JSON | `{jobId,keyframeId}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/copy-transition-style` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/duplicate-transition-video` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-keyframe-label` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-transition-label` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-keyframe-style` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-transition-style` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/assign-keyframe-image` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/transition-effects/add` | yes | JSON | `{effect}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/transition-effects/update` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/transition-effects/delete` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/save-as-still` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/markers/add` | yes | JSON | `{marker}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/markers/update` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/markers/remove` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/prompt-roster/add` | yes | JSON | `{prompt}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/prompt-roster/update` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/prompt-roster/remove` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/split-transition` | yes | JSON | `{ok}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/insert-pool-item` | yes | JSON | `{ok}` | 200, 400, 404 | **Y** | |
| POST | `/api/projects/:name/generate-slot-keyframe-candidates` | yes | JSON | `{jobId}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/generate-keyframe-candidates` | yes | JSON | `{jobId}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/generate-transition-candidates` | yes | JSON | `{jobId}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/ingredients/promote` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/ingredients/remove` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/ingredients/update` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/extend-video` | yes | JSON | `{jobId}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/update-meta` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/effects` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/import` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/settings` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/watch-folder` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/unwatch-folder` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/narrative` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/branches` | yes | JSON | `{branch}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/branches/delete` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/checkout` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/version/commit` | yes | JSON | deprecated no-op | 200 | — | |
| POST | `/api/projects/:name/version/checkout` | yes | JSON | deprecated | 200 | — | |
| POST | `/api/projects/:name/version/branch` | yes | JSON | deprecated | 200 | — | |
| POST | `/api/projects/:name/version/delete-branch` | yes | JSON | deprecated | 200 | — | |
| POST | `/api/projects/:name/promote-staged-candidate` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/generate-staged-candidate` | yes | JSON | `{jobId}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/suggest-keyframe-prompts` | yes | JSON | `{prompts:[…]}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/enhance-keyframe-prompt` | yes | JSON | `{prompt}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/section-settings` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/track-effects` | yes | JSON | `{effect}` | 200, 400, 404 | — | M13 create |
| POST | `/api/projects/:name/track-effects/:id` | yes | JSON | `{ok}` | 200, 400, 404 | — | M13 update |
| POST | `/api/projects/:name/effect-curves` | yes | JSON | `{curve}` | 200, 400, 404 | — | M13 create |
| POST | `/api/projects/:name/effect-curves/batch` | yes | JSON | `{curves:[…]}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/effect-curves/:id` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/send-buses` | yes | JSON | `{bus}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/send-buses/:id` | yes | JSON | `{ok}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/track-sends` | yes | JSON | `{send}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/frequency-labels` | yes | JSON | `{label}` | 200, 400, 404 | — | |
| POST | `/api/projects/:name/plugins/:plugin/*` | yes | JSON | plugin JSON | 200, 404, 500 | — | Plugin fallback |

### DELETE

| Method | Path | Auth | Body | Response | Status codes | Lock | Notes |
|---|---|---|---|---|---|---|---|
| DELETE | `/api/projects/:name/track-effects/:id` | yes | — | empty / `{ok}` | 200 | — | **Idempotent: 200 on missing** |
| DELETE | `/api/projects/:name/effect-curves/:id` | yes | — | empty / `{ok}` | 200 | — | Idempotent |
| DELETE | `/api/projects/:name/send-buses/:id` | yes | — | empty / `{ok}` | 200 | — | Idempotent |
| DELETE | `/api/projects/:name/frequency-labels/:id` | yes | — | empty / `{ok}` | 200 | — | Idempotent |

### HEAD / OPTIONS

| Method | Path | Auth | Body | Response | Status codes | Notes |
|---|---|---|---|---|---|---|
| HEAD | `/api/projects/:name/files/*` | none (no auth check) | — | empty + headers | 200, 404 | Browser video preload |
| HEAD | any other path | — | — | empty | 405 | |
| OPTIONS | any path | none | — | empty | 204 | CORS preflight |

---

## Behavior Table

Behavior rows cover the dispatcher contract (cross-cutting concerns + representative routes). Routes not enumerated individually below are covered by template rows (happy, project-404, bad-body) that apply to every route in the catalog of the same shape.

| # | Scenario | Expected Behavior | Tests |
|---|---|---|---|
| 1 | GET `/api/projects` with valid bearer token | 200 JSON project list | `get-projects-with-bearer` |
| 2 | GET `/api/projects` with valid cookie | 200 + refreshed Set-Cookie | `get-projects-with-cookie-sliding-refresh` |
| 3 | GET `/api/projects` with NO token (auth enabled) | 401 `{error, code:"UNAUTHORIZED"}` | `unauth-missing-token-401` |
| 4 | GET `/api/projects` with expired bearer | 401 `{code:"UNAUTHORIZED"}` | `unauth-expired-token-401` |
| 5 | GET `/api/projects` with malformed bearer | 401 `{code:"UNAUTHORIZED"}` | `unauth-malformed-bearer-401` |
| 6 | Request carries both bearer and cookie | Bearer wins; no cookie refresh on response | `bearer-precedence-over-cookie` |
| 7 | `.scenecraft` root absent (auth disabled) | 200 without any token | `no-auth-mode-passes-through` |
| 8 | GET `/auth/login?code=<valid>` | 303 Location=`/`, Set-Cookie set | `auth-login-valid-code-303` |
| 9 | GET `/auth/login?code=<expired>` | 401 `{code:"INVALID_CODE"}` | `auth-login-expired-code-401` |
| 10 | GET `/auth/login` no code | 400 `{code:"BAD_REQUEST"}` | `auth-login-missing-code-400` |
| 11 | GET `/auth/login` when auth disabled | 501 `{code:"AUTH_DISABLED"}` | `auth-login-disabled-501` |
| 12 | POST `/auth/logout` | 200 `{ok:true}` + Set-Cookie clearing | `auth-logout-clears-cookie` |
| 13 | GET `/oauth/callback?code=X&state=Y` valid | 200 HTML (popup-close) | `oauth-callback-success-html` |
| 14 | GET `/oauth/callback` with `?error=access_denied` | 200 HTML with error message | `oauth-callback-error-html` |
| 15 | GET `/api/oauth/unknown/authorize` | 404 `{code:"UNKNOWN_SERVICE"}` | `oauth-authorize-unknown-service` |
| 16 | GET `/api/oauth/:svc/authorize` valid | 200 `{url, state}` | `oauth-authorize-returns-url` |
| 17 | OPTIONS any path | 204 with CORS headers | `options-preflight-204` |
| 18 | Request with `Origin: https://app.example.com` | Response echoes origin, ACAC=true, Vary:Origin | `cors-echoes-origin-with-credentials` |
| 19 | Request without Origin header | Response uses `ACAO: *`, no Vary, no ACAC | `cors-wildcard-when-no-origin` |
| 20 | Request with `Origin: https://evil.example` + cookie | Origin is echoed; credentials=true — XSRF-exposed | `cors-no-allowlist-xsrf-exposure` |
| 21 | Unknown path GET `/api/nope` | 404 `{error:"No route: GET /api/nope", code:"NOT_FOUND"}` | `unknown-route-404` |
| 22 | Unknown path POST | 404 `{code:"NOT_FOUND", error:"No route: POST …"}` | `unknown-post-route-404` |
| 23 | GET project that does not exist | 404 `{code:"NOT_FOUND"}` | `project-not-found-404` |
| 24 | POST with empty body (Content-Length 0) | 400 `{code:"BAD_REQUEST", error:"Empty body"}` | `empty-body-400` |
| 25 | POST with invalid JSON body | 400 `{code:"BAD_REQUEST", error starts with "Invalid JSON:"}` | `bad-json-400` |
| 26 | POST with extra unknown fields | Handler tolerates; 200 (dispatcher passes through) | `extra-fields-tolerated` |
| 27 | Concurrent POSTs to `/add-keyframe` same project | Serialized via per-project lock; both succeed sequentially | `structural-lock-serializes` |
| 28 | Concurrent POSTs to `/add-keyframe` different projects | Run in parallel | `structural-lock-is-per-project` |
| 29 | Concurrent POSTs to `/update-prompt` same project | Race freely — NO lock | `non-locked-routes-race-freely` |
| 30 | POST `/delete-keyframe` handler raises | Lock is released, client gets 500, no deadlock | `lock-released-on-handler-exception` |
| 31 | POST `/split-transition` produces orphan transition | 200 + warning logged + WS `timeline_warning` broadcast | `timeline-validation-warns-non-blocking` |
| 32 | POST `/update-prompt` leaves DB inconsistent | No timeline validation invoked (non-locked route) | `no-validation-on-non-locked-route` |
| 33 | GET `/api/projects/:p/files/existing.mp4` full | 200 + ETag + Last-Modified + chunked body | `file-serve-full-200` |
| 34 | GET files with `Range: bytes=0-1023` | 206 Partial Content, Content-Range `bytes 0-1023/<size>` | `file-serve-range-206` |
| 35 | GET files with `Range: bytes=0-` (open-ended) | 206 with Content-Length == file size; chunked write (no OOM) | `file-serve-open-ended-range-chunked` |
| 36 | GET files with matching `If-None-Match` | 304 Not Modified, empty body | `file-serve-etag-304` |
| 37 | GET files with fresh `If-Modified-Since` | 304 | `file-serve-ims-304` |
| 38 | GET files with malformed `If-Modified-Since` | 200 full (fallback) | `file-serve-ims-malformed-fallback` |
| 39 | GET `/api/projects/:p/files/../../etc/passwd` | 403 `{code:"FORBIDDEN", error:"Path traversal denied"}` | `file-serve-traversal-403` |
| 40 | GET files where target is a symlink to `/etc/passwd` | `undefined` — startswith guard does not re-check after readlink | → [OQ-3](#open-questions) |
| 41 | GET files missing | 404 `{code:"NOT_FOUND"}` | `file-serve-missing-404` |
| 42 | HEAD `/api/projects/:p/files/existing` | 200 + headers, empty body | `head-existing-file-200` |
| 43 | HEAD missing file | 404 empty body (NOT `{error,code}`) | `head-missing-empty-404` |
| 44 | HEAD arbitrary other path | 405 empty body | `head-unknown-path-405` |
| 45 | DELETE `/track-effects/:nonexistent-id` | 200 (idempotent, M13 task-52) | `delete-nonexistent-effect-idempotent` |
| 46 | DELETE `/effect-curves/:nonexistent-id` | 200 idempotent | `delete-nonexistent-curve-idempotent` |
| 47 | DELETE `/send-buses/:nonexistent-id` | 200 idempotent | `delete-nonexistent-bus-idempotent` |
| 48 | DELETE `/frequency-labels/:nonexistent-id` | 200 idempotent | `delete-nonexistent-label-idempotent` |
| 49 | DELETE unknown DELETE path | 404 `{code:"NOT_FOUND"}` | `delete-unknown-path-404` |
| 50 | GET `/api/projects/:p/plugins/:plugin/foo` matches registered plugin route | 200 plugin JSON | `plugin-get-dispatch-success` |
| 51 | GET `/api/projects/:p/plugins/:plugin/foo` no matching pattern | 404 `{code:"NOT_FOUND"}` | `plugin-get-no-match-404` |
| 52 | POST plugin route handler raises | 500 `{code:"PLUGIN_ERROR", error: "<exc str>"}` | `plugin-handler-exception-500` |
| 53 | POST plugin with empty body | Dispatcher passes `{}` (not 400) | `plugin-empty-body-defaulted-to-object` |
| 54 | Plugin route with named regex groups `(?P<id>\d+)` | Handler receives `path_groups={id:"…"}` | `plugin-named-groups-propagated` |
| 55 | Plugin route without named groups | Handler receives NO `path_groups` kwarg | `plugin-no-groups-no-kwarg` |
| 56 | DELETE plugin route | `undefined` — dispatcher only forwards GET/POST to plugin fallback | → [OQ-4](#open-questions) |
| 57 | Any endpoint decorated `@require_paid_plugin_auth` | Currently none exist; behavior is `undefined` as applied | → [OQ-1](#open-questions) |
| 58 | Paid-plugin handler: valid JWT + valid API key | 200 + `_paid_auth_ctx` attached (tested via unit test of the decorator) | `paid-auth-happy-path` |
| 59 | Paid-plugin handler: missing `X-Scenecraft-API-Key` | 401 `{code:"UNAUTHORIZED"}` | `paid-auth-missing-key-401` |
| 60 | Paid-plugin handler: unknown API key | 401 `{code:"UNAUTHORIZED"}` | `paid-auth-bad-key-401` |
| 61 | Paid-plugin handler: `must_change_password=1` | 403 `{code:"PASSWORD_CHANGE_REQUIRED"}` | `paid-auth-password-change-403` |
| 62 | Paid-plugin handler: `X-Scenecraft-Org` user not member | 400 `{code:"ORG_NOT_FOUND"}` | `paid-auth-bad-org-400` |
| 63 | Paid-plugin handler: no org header, multiple memberships | 400 `{code:"AMBIGUOUS_ORG"}` | `paid-auth-ambiguous-org-400` |
| 64 | Paid-plugin handler: no org header, exactly one membership | Succeeds, resolves to the single org | `paid-auth-single-org-fallback` |
| 65 | Response shape on 401 | `{error,code}` — NOT FastAPI default `{detail}` | `error-shape-preserved-401` |
| 66 | Response shape on 500 (handler raised) | `{error,code:"INTERNAL_ERROR"}` or `{code:"PLUGIN_ERROR"}` (plugin) | `error-shape-preserved-500` |
| 67 | Response carries `Content-Length` on JSON | Always; equals `len(json.dumps(obj).encode())` | `json-content-length-set` |
| 68 | Client disconnects mid-response | No exception logged; dispatcher swallows BrokenPipeError | `client-disconnect-swallowed` |
| 69 | `/api/_internal/broadcast` missing `type` | 400 `{code:"BAD_REQUEST", error:"missing 'type'"}` | `internal-broadcast-missing-type-400` |
| 70 | `/api/_internal/broadcast` valid | 200 `{ok:true}` + WS broadcast | `internal-broadcast-ok` |
| 71 | `/api/_internal/broadcast` not exempt from auth | 401 without token (auth enabled) | `internal-broadcast-requires-auth` |
| 72 | GET `/api/render-cache/stats` | 200 stats | `render-cache-stats-200` |
| 73 | POST `/api/config` bad body | 400 | `config-post-bad-body-400` |
| 74 | POST `/api/projects/create` valid | 200 created payload | `create-project-200` |
| 75 | GET `/api/browse?path=../..` | 403 `{code:"FORBIDDEN"}` | `browse-traversal-403` |
| 76 | GET `/api/browse?path=nonexistent` | 404 | `browse-missing-404` |
| 77 | GET `/api/browse` default path | 200 work_dir root listing | `browse-root-200` |
| 78 | Upload multipart `pool/upload` happy | 200 `{segment}` | `pool-upload-multipart-200` |
| 79 | Upload multipart missing file part | 400 `{code:"BAD_REQUEST"}` | `pool-upload-missing-part-400` |
| 80 | `bounce-upload` happy | 200 `{bounceId}` + WAV on disk | `bounce-upload-200` |
| 81 | `bounce-upload` invalid WAV header | 400 + file deleted | `bounce-upload-invalid-wav-400` |
| 82 | `mix-render-upload` happy | 200 | `mix-render-upload-200` |
| 83 | `bench/upload` happy | 200 | `bench-upload-200` |
| 84 | POST body exceeds N MB | `undefined` — no enforced cap | → [OQ-5](#open-questions) |
| 85 | Concurrent uploads to same `pool/upload` path | `undefined` — no explicit mutex, filesystem last-writer-wins | → [OQ-6](#open-questions) |
| 86 | URL with trailing slash `/api/projects/` | 404 `{code:"NOT_FOUND"}` | `trailing-slash-404` |
| 87 | URL-encoded project name `/api/projects/my%20proj/keyframes` | Handler sees decoded name `"my proj"` | `url-decoded-project-name` |
| 88 | Project name contains `..` | 404 on project dir resolve (no match against real dir) | `project-name-dotdot-404` |
| 89 | `Authorization: Basic …` | 401 (only Bearer scheme recognized) | `non-bearer-scheme-401` |
| 90 | Cookie named wrong (`jwt=…` without `scenecraft_jwt`) | 401 (no token extracted) | `wrong-cookie-name-401` |
| 91 | Two cookies: invalid scenecraft_jwt first, valid second | Valid extracted (first match wins per Cookie spec) — `undefined` which one | → [OQ-7](#open-questions) |
| 92 | Cookie refresh fails (DB error mid-request) | Original response still succeeds; no Set-Cookie | `cookie-refresh-failure-graceful` |
| 93 | 304 response | Includes CORS + ETag; no body | `304-has-cors-and-etag` |
| 94 | Range with `end < start` | `undefined` — current regex accepts it | → [OQ-8](#open-questions) |
| 95 | Range past EOF (`bytes=99999-`) | Server clamps `end = file_size - 1`; Content-Length may be negative | `range-past-eof-clamped` |
| 96 | Multiple Range headers (e.g. `0-100,200-300`) | `undefined` — regex only matches the first | → [OQ-9](#open-questions) |
| 97 | Very long URL (>8KB) | `undefined` — Python stdlib limits apply | → [OQ-10](#open-questions) |
| 98 | Handler returns non-JSON-serializable value | Raises, caught as 500 INTERNAL_ERROR | `handler-returns-unserializable-500` |
| 99 | Two concurrent cookie-refresh races | Both get fresh cookies; either is valid | `concurrent-cookie-refresh-both-valid` |
| 100 | POST `/checkpoint` validation warns | 200 + WS warning; response not affected | `checkpoint-validation-warn-passthrough` |
| 101 | Plugin REST: registered pattern contains `\d+` (numeric id) | Non-numeric id falls through; 404 if nothing else matches | `plugin-regex-numeric-constraint` |
| 102 | Same plugin registers same pattern twice | Second registration overwrites first | `plugin-duplicate-pattern-overwrites` |
| 103 | Dispatcher receives unsupported HTTP method (PATCH on existing path) | 501 (stdlib) or 405 | `unsupported-method-handled` |
| 104 | Request with body on GET | Body ignored; handler sees only path + query | `get-with-body-ignored` |
| 105 | OPTIONS when auth enabled and no token | 204 still (OPTIONS bypasses auth) | `options-bypasses-auth` |
| 106 | Sliding refresh after bearer auth | NO Set-Cookie (bearer not refreshed) | `bearer-no-cookie-refresh` |
| 107 | Response to bearer-authed request carries cookies from other request state | MUST NOT — `_refreshed_cookie` only set when this request used cookie | `no-leak-cookie-on-bearer-request` |
| 108 | Any 401 response | Contains `code:"UNAUTHORIZED"` — never 403 | `missing-auth-is-401-not-403` |
| 109 | Any 403 response | Reserved for path traversal + `PASSWORD_CHANGE_REQUIRED` only | `403-reserved-for-forbidden-semantics` |
| 110 | CORS `Access-Control-Allow-Headers` includes `X-Scenecraft-Branch` | Always | `cors-allows-x-scenecraft-branch` |
| 111 | CORS `Access-Control-Allow-Headers` includes `X-Scenecraft-API-Key` | `undefined` — not in current list | → [OQ-11](#open-questions) |
| 112 | All enumerated GET endpoints with project-not-found project | 404 `{code:"NOT_FOUND"}` | `all-get-routes-project-404` (parametric) |
| 113 | All enumerated POST endpoints with empty body | 400 `{code:"BAD_REQUEST"}` (except plugin routes) | `all-post-routes-empty-body-400` (parametric) |
| 114 | All enumerated POST endpoints with bad JSON | 400 `{code:"BAD_REQUEST"}` | `all-post-routes-bad-json-400` (parametric) |
| 115 | All enumerated endpoints without token (auth on) | 401 `{code:"UNAUTHORIZED"}` | `all-routes-require-auth-except-exempts` (parametric) |
| 116 | Exempt paths don't require auth | 200/303/HTML as applicable | `exempt-paths-no-auth-needed` (parametric over `/auth/login`, `/auth/logout`, `/oauth/callback`) |
| 117 | Every error emitted by source uses `{error,code}` shape | Every one, across all ~40 code strings | `every-error-uses-standard-shape` (parametric) |
| 118 | GET cached file: Cache-Control set | `public, max-age=3600, immutable` | `file-cache-control-immutable` |
| 119 | HEAD on file: path traversal | `undefined` — HEAD uses same startswith check, behavior symmetrical to GET | `head-traversal-404` |
| 120 | GET `/api/projects/:p/files/*` with query string | Query ignored; file served normally | `file-serve-ignores-query-string` |
| 121 | Handler emits 200 but then raises mid-write | Swallowed (BrokenPipe path); partial body delivered | `handler-raises-mid-write-swallowed` |
| 122 | Locked-route handler success + validation raises | Validation exception logged, response still 200 | `validation-exception-does-not-affect-response` |
| 123 | JWT secret rotated mid-session | Existing cookies invalidated → 401 | `jwt-secret-rotation-invalidates-cookies` |
| 124 | Sliding refresh retains original `last_active_org` claim | `undefined` — `generate_token` uses DB row, not old payload | → [OQ-12](#open-questions) |
| 125 | Bearer token past 24h hard expiry | 401 (no refresh path exists for bearer) | `bearer-hard-expiry-24h` |
| 126 | Request on `/auth/login` carries cookie | Cookie ignored (exempt path skips auth) | `exempt-ignores-cookie` |
| 127 | Request logs contain JWT | MUST NOT — JWT is not in `_log` output | `no-jwt-in-logs` |
| 128 | Response logs contain Set-Cookie | MUST NOT | `no-cookie-in-logs` |
| 129 | Non-existent plugin id in path | 404 `{code:"NOT_FOUND"}` (no plugin pattern matches) | `plugin-unknown-id-404` |
| 130 | Plugin dispatch when `project_dir` missing | 404 `{code:"NOT_FOUND"}` before plugin invocation | `plugin-project-dir-check-first` |
| 131 | Timeline validation finds >10 warnings | First 10 logged, ALL broadcast over WS | `timeline-validation-log-cap-10` |
| 132 | Dispatcher receives `Expect: 100-continue` | `undefined` — not explicitly handled | → [OQ-13](#open-questions) |
| 133 | Dispatcher receives chunked transfer encoding POST | `undefined` — `_read_json_body` only reads `Content-Length` bytes | → [OQ-14](#open-questions) |
| 134 | Bearer token with different signing algorithm | 401 (`validate_token` only accepts HS256) | `bearer-wrong-alg-401` |
| 135 | Bearer token with `alg: none` | 401 | `bearer-alg-none-rejected-401` |
| 136 | Duplicate path + method registration within builtin routes | First match wins (top-down in do_GET/do_POST) | `builtin-first-match-wins` |
| 137 | Plugin route overlaps builtin path | Builtin wins; plugin fallback only runs when builtin 404s for that path class | `builtin-beats-plugin` |
| 138 | Sliding cookie refresh on DELETE request | Refreshes cookie same as GET/POST | `delete-cookie-refreshes` |
| 139 | DELETE request with body | Body ignored | `delete-body-ignored` |
| 140 | 404 response for unknown method | Status is 404, not 405 — dispatcher only emits 405 from HEAD | `unknown-method-not-405` |
| 141 | Concurrent requests: HTTP server is ThreadingMixIn | Each request in its own thread; per-project lock serializes structural routes | `threadingmixin-parallelism` |
| 142 | `_require_project_dir` for a path that exists as a file (not dir) | 404 `{code:"NOT_FOUND"}` | `project-dir-is-file-404` |
| 143 | Handler writes non-`{ok}` dict; field names stable across FastAPI port | Exact keys preserved per handler spec | `field-names-frozen` (parametric) |
| 144 | GET with query `?limit=NaN` on chat | `ValueError` caught, default 50 used | `chat-bad-limit-defaults-50` |
| 145 | GET `/api/projects/:p/chat` without limit | Defaults to 50 messages | `chat-default-limit-50` |
| 146 | GET `/api/projects/:p/workspace-views/:unknown` | 404 `{code:"NOT_FOUND"}` | `workspace-view-unknown-404` |
| 147 | POST `/api/projects/:p/workspace-views/:view/delete` when missing | `undefined` — depends on DAL idempotency | → [OQ-15](#open-questions) |
| 148 | Status 501 only for `AUTH_DISABLED` on `/auth/login` | Canonical use | `501-reserved-for-auth-disabled` |
| 149 | Response `Content-Type` for error | `application/json` | `error-content-type-json` |
| 150 | Response `Content-Type` for HTML callback | `text/html; charset=utf-8` | `oauth-callback-content-type-html` |
| 151 | Browser preload HEAD against video | 200 with `Accept-Ranges: bytes` | `head-video-accept-ranges` |
| 152 | 206 response ordering of headers | `Content-Range` MUST appear | `206-content-range-required` |
| 153 | 304 response body size | Exactly 0 bytes | `304-empty-body` |
| 154 | GET `/api/projects` after project deleted between list and fetch | Subsequent fetch returns 404 (race acknowledged, not prevented) | `project-deleted-race-404` |
| 155 | Two parallel `delete-keyframe` for same kf_id | First wins; second runs under lock, sees already-deleted state | `delete-keyframe-double-delete` |
| 156 | 500 emitted with `{code:"INTERNAL_ERROR"}` | Dispatcher-level catch of unexpected exceptions | `internal-error-500` |
| 157 | Uncaught exception in handler | `undefined` — current dispatcher has NO top-level try/except around handlers (only plugin fallback); stdlib returns 500 with empty body | → [OQ-16](#open-questions) |
| 158 | Sliding refresh keeps user authenticated beyond 24h | Yes (cookie-only path) | `sliding-refresh-extends-session` |
| 159 | Bearer token 24h expiry hits mid-long-running upload | Handler already authenticated; request completes | `auth-check-at-request-start-only` |
| 160 | Response status on successful delete of existing effect | 200 | `delete-existing-effect-200` |
| 161 | Request with `Content-Length` larger than body | Handler reads fewer bytes than expected → JSON parse error → 400 | `truncated-body-400` |
| 162 | Request with `Content-Length` absent | Treated as 0 → 400 `"Empty body"` | `missing-content-length-400` |
| 163 | Response served with `Transfer-Encoding: chunked` | NOT used — dispatcher always sets `Content-Length` for JSON | `no-chunked-encoding-for-json` |
| 164 | File serving sends chunked bytes but with Content-Length (not TE) | Yes — explicit Content-Length + raw write | `file-serve-no-te-chunked` |
| 165 | WS `/ws/chat/:project` | Not served by dispatcher; handled by ws_server | `ws-path-not-dispatched` |
| 166 | OPTIONS for `/ws/chat/*` | 204 (OPTIONS is blanket) | `options-for-ws-path-204` |
| 167 | Every current error `code` string present in migration's handler | Every one — catalog is frozen | `error-code-catalog-preserved` |
| 168 | Dispatcher swallows `BrokenPipe` on error response | Never re-raises | `error-response-broken-pipe-swallowed` |
| 169 | GET `/api/projects/:p/plugins/unknown_plugin/x` (plugin not loaded) | 404 (no matching pattern) | `plugin-not-loaded-404` |
| 170 | POST to plugin with kwargs the handler doesn't accept | 500 PLUGIN_ERROR (TypeError caught) | `plugin-bad-signature-500` |
| 171 | Dispatcher URL-decodes path before regex match | Yes (`unquote(parsed.path)`) | `url-decoded-before-routing` |
| 172 | Project name with `%2F` (encoded slash) | After decode contains `/`; regex `[^/]+` fails to match — 404 | `encoded-slash-in-project-404` |
| 173 | Simultaneous 200 + Timeline Warning WS broadcast | Response returned first, broadcast async-safe | `ordering-response-then-broadcast` |
| 174 | Empty `Origin` header | Treated as absent; wildcard ACAO applied | `empty-origin-treated-absent` |
| 175 | OPTIONS response includes `Access-Control-Max-Age` | `undefined` — current code does not set it | → [OQ-17](#open-questions) |
| 176 | Response `Set-Cookie` has `SameSite=Lax` | Always | `cookie-samesite-lax` |
| 177 | Response `Set-Cookie` has `Secure` flag | Only when configured by deployment (default: absent) | `cookie-secure-optional` |
| 178 | Response `Set-Cookie` has `HttpOnly` | Always | `cookie-httponly-always` |
| 179 | JWT sub claim with special characters | Passed through to `_authenticated_user` unchanged | `jwt-sub-preserved` |
| 180 | JWT missing `sub` claim after validation | `undefined` — dispatcher treats as authenticated with `None` user; downstream may misbehave | → [OQ-18](#open-questions) |
| 181 | Two requests with identical `Set-Cookie` refresh content | Both are valid JWTs; either accepted | `refresh-cookie-independence` |
| 182 | POST empty object body `{}` | Handler decides; dispatcher returns 200 from `_read_json_body` | `empty-object-body-passes-dispatcher` |

---

## Behavior (Dispatcher pipeline)

For every request, in order:

1. **Method dispatch** — `do_<METHOD>` picks the method branch. `OPTIONS` short-circuits to 204 + CORS. `HEAD` routes to the files-only HEAD handler or 405.
2. **Auth** — `_authenticate()`:
   - If `_sc_root is None`, return True (auth disabled mode).
   - If path in `{/auth/login, /auth/logout, /oauth/callback}`, return True.
   - Extract bearer → else cookie → else 401 `UNAUTHORIZED`.
   - `validate_token`; on failure 401; on success set `_authenticated_user`; on cookie path mint a fresh token into `_refreshed_cookie`.
3. **Routing** — sequential `re.match` against built-in routes per the catalog. First match wins. Each handler may call `_require_project_dir` (→ 404 `NOT_FOUND` if missing) or `_read_json_body` (→ 400 `BAD_REQUEST` on empty/invalid).
4. **Structural lock (POST only)** — if the POST path's last segment is one of the 11 structural routes, acquire the per-project lock before `_do_POST`. After `_do_POST` returns (or raises), run `validate_timeline` and broadcast warnings. Release lock in `finally`.
5. **Plugin fallback** — if no built-in route matched and path is `/api/projects/:p/plugins/:plugin/*`, forward to `PluginHost.dispatch_rest`. None → continue to 404. Value → `_json_response(value)`. Exception → `500 PLUGIN_ERROR`.
6. **Response** — `_json_response(obj, status)` emits Content-Type, Content-Length, CORS, optional Set-Cookie (from refreshed cookie), then body. File responses emit their own headers per Range/ETag/IMS logic.
7. **404** — `self._error(404, "NOT_FOUND", f"No route: {METHOD} {path}")` is emitted as a last resort in each `do_<METHOD>`.

---

## Acceptance Criteria

- [ ] All 180+ Behavior Table rows translate into HTTP-level tests; all pass against the current `api_server.py`.
- [ ] The same suite is portable to the FastAPI rewrite (no tests reach into Python internals or mock handler classes directly).
- [ ] Error body shape `{error, code}` is preserved on every error path.
- [ ] Every endpoint in the Full Endpoint Catalog has at least a happy-path, project-404, and (for POST) empty-body/bad-JSON test.
- [ ] Every `undefined` row has a matching Open Question.
- [ ] No test asserts on internal implementation (threading.Lock object identity, regex object identity, etc.) — only on HTTP-observable behavior + DB state + WS broadcast presence.

---

## Tests

### Base Cases

The tests here define the HTTP-level contract. Implementations translate each into their framework (pytest+requests against a live test server is the default).

#### Test: get-projects-with-bearer (covers R1, R6, R8)

**Given**: auth enabled; test user exists; valid JWT `T` issued for that user
**When**: GET `/api/projects` with `Authorization: Bearer T`
**Then**:
- **status-200**: response status is 200
- **body-shape**: body is a JSON object with key `projects` whose value is a list
- **no-set-cookie**: response does NOT contain `Set-Cookie` (bearer path doesn't refresh)
- **cors-origin**: response includes `Access-Control-Allow-Origin: *` (no Origin sent)

#### Test: get-projects-with-cookie-sliding-refresh (covers R11)

**Given**: valid JWT `T` issued ≥ 1 minute ago; client sends it as cookie
**When**: GET `/api/projects` with `Cookie: scenecraft_jwt=T`
**Then**:
- **status-200**: 200
- **set-cookie-present**: response has `Set-Cookie: scenecraft_jwt=<T'>; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400`
- **token-differs**: `T' != T`
- **token-valid**: `T'` passes `validate_token`
- **sub-preserved**: decoded `T'` has same `sub` as `T`

#### Test: unauth-missing-token-401 (covers R9)

**Given**: auth enabled
**When**: GET `/api/projects` with no Authorization, no Cookie
**Then**:
- **status-401**: 401
- **body-code**: body has `code: "UNAUTHORIZED"`
- **body-error**: body has `error: "Not authenticated"`
- **cors-headers-present**: CORS headers still emitted

#### Test: bearer-precedence-over-cookie (covers R8)

**Given**: valid bearer `TB`, and cookie `TC` with a *different* sub
**When**: GET `/api/projects` with both headers
**Then**:
- **status-200**: 200
- **user-is-bearer**: log or debug surface indicates `TB`'s sub (e.g. a `GET /api/_debug/whoami` pseudo-endpoint in tests; concretely via an authed handler that echoes `_authenticated_user`)
- **no-refresh**: no Set-Cookie on response

#### Test: no-auth-mode-passes-through (covers R13)

**Given**: server started with `--no-auth` OR no `.scenecraft` root
**When**: GET `/api/projects` without any token
**Then**:
- **status-200**: 200
- **body-shape-valid**: `projects` key present

#### Test: options-preflight-204 (covers R17)

**Given**: auth enabled, no token
**When**: OPTIONS `/api/projects/any`
**Then**:
- **status-204**: 204
- **cors-methods**: `Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS`
- **cors-headers-list**: `Access-Control-Allow-Headers` includes `Content-Type`, `Authorization`, `X-Scenecraft-Branch`
- **body-empty**: zero bytes

#### Test: cors-echoes-origin-with-credentials (covers R16)

**Given**: auth enabled; valid token; `Origin: https://app.example.com`
**When**: GET `/api/projects`
**Then**:
- **acao-echoes**: `Access-Control-Allow-Origin: https://app.example.com`
- **acac-true**: `Access-Control-Allow-Credentials: true`
- **vary-origin**: `Vary: Origin`

#### Test: cors-wildcard-when-no-origin (covers R16)

**Given**: valid token, no Origin header
**When**: GET `/api/projects`
**Then**:
- **acao-wildcard**: `Access-Control-Allow-Origin: *`
- **acac-absent**: no `Access-Control-Allow-Credentials` header
- **vary-absent**: no `Vary: Origin` header

#### Test: cors-no-allowlist-xsrf-exposure (covers R18)

**Given**: valid cookie `T`; `Origin: https://evil.example`
**When**: GET `/api/projects`
**Then**:
- **acao-echoed-evil**: response has `Access-Control-Allow-Origin: https://evil.example`
- **acac-true**: `Access-Control-Allow-Credentials: true`
- **xsrf-flag**: test is annotated as a known-exposure baseline; FastAPI port MUST match until [OQ-2](#open-questions) resolves

#### Test: unknown-route-404 (covers R1)

**Given**: valid token
**When**: GET `/api/definitely-not-a-route`
**Then**:
- **status-404**: 404
- **code-not-found**: body `code: "NOT_FOUND"`
- **message-includes-method**: `error` contains `"GET /api/definitely-not-a-route"`

#### Test: project-not-found-404 (covers R1)

**Given**: valid token; project `does-not-exist` missing from work_dir
**When**: GET `/api/projects/does-not-exist/keyframes`
**Then**:
- **status-404**: 404
- **code-not-found**: `NOT_FOUND`

#### Test: empty-body-400 (covers R19)

**Given**: valid token
**When**: POST `/api/projects/p/update-prompt` with `Content-Length: 0`
**Then**:
- **status-400**: 400
- **code-bad-request**: `BAD_REQUEST`
- **error-empty-body**: error string equals `"Empty body"`

#### Test: bad-json-400 (covers R19)

**Given**: valid token
**When**: POST with body `not-json` and correct Content-Length
**Then**:
- **status-400**: 400
- **error-prefix**: error starts with `"Invalid JSON:"`

#### Test: structural-lock-serializes (covers R28, R29)

**Given**: project `p`; two concurrent POSTs to `/api/projects/p/add-keyframe` from two clients
**When**: both launched simultaneously
**Then**:
- **both-200**: both return 200
- **serial-observations**: handlers' internal timestamps show non-overlapping lock-held windows (observed via instrumentation hook or DB insert timestamps)

#### Test: structural-lock-is-per-project (covers R29)

**Given**: projects `p1` and `p2`
**When**: POST `add-keyframe` to both concurrently
**Then**:
- **parallel-timing**: both complete in roughly `max(t1,t2)`, not `t1+t2`

#### Test: non-locked-routes-race-freely (covers R30)

**Given**: project `p`; two concurrent POSTs to `/update-prompt`
**When**: sent simultaneously
**Then**:
- **overlapping-execution**: both handlers observably run concurrently (race-freely baseline)

#### Test: lock-released-on-handler-exception (covers R32)

**Given**: project `p`; handler `add-keyframe` patched to raise
**When**: first request raises, second request sent immediately after
**Then**:
- **first-is-500**: first response is 500
- **second-acquires-quickly**: second request does NOT time out waiting for the lock

#### Test: timeline-validation-warns-non-blocking (covers R31, R49)

**Given**: project state that will fail validation after `split-transition`
**When**: POST `/split-transition`
**Then**:
- **status-200**: handler's normal 200
- **log-warning**: `_log` observes `⚠ Timeline validation` line
- **ws-broadcast**: `job_manager._broadcast` called with `type: "timeline_warning"` and route `split-transition`

#### Test: no-validation-on-non-locked-route (covers R49)

**Given**: non-locked route `update-prompt`
**When**: POST it
**Then**:
- **no-validation**: `validate_timeline` is NOT invoked

#### Test: file-serve-full-200 (covers R40)

**Given**: file `assets/v1.mp4` exists in project `p`
**When**: GET `/api/projects/p/files/assets/v1.mp4`
**Then**:
- **status-200**: 200
- **content-length-matches**: equals file size
- **etag**: ETag header present in `"<hex>-<hex>"` form
- **accept-ranges**: `Accept-Ranges: bytes`
- **cache-control**: `public, max-age=3600, immutable`

#### Test: file-serve-range-206 (covers R38)

**Given**: same file, size ≥ 1024
**When**: GET with `Range: bytes=0-1023`
**Then**:
- **status-206**: 206
- **content-length-1024**: `Content-Length: 1024`
- **content-range**: `Content-Range: bytes 0-1023/<size>`
- **body-bytes**: 1024 bytes

#### Test: file-serve-open-ended-range-chunked (covers R39)

**Given**: file size 100MB
**When**: GET with `Range: bytes=0-`
**Then**:
- **status-206**: 206
- **full-length**: `Content-Length: 104857600`
- **memory-bound**: process RSS does not grow by 100MB (chunked write)

#### Test: file-serve-etag-304 (covers R36)

**Given**: prior GET returned ETag `"E"`
**When**: GET same path with `If-None-Match: "E"`
**Then**:
- **status-304**: 304
- **body-empty**: zero-length body
- **etag-echoed**: ETag header same `"E"`

#### Test: file-serve-ims-304 (covers R37)

**Given**: file mtime `M`
**When**: GET with `If-Modified-Since: <M+1s>` in RFC 2822 format
**Then**:
- **status-304**: 304

#### Test: file-serve-traversal-403 (covers R33)

**Given**: work_dir `/work`; project `p`
**When**: GET `/api/projects/p/files/../../etc/passwd`
**Then**:
- **status-403**: 403
- **code-forbidden**: `FORBIDDEN`
- **error-text**: "Path traversal denied"

#### Test: delete-nonexistent-effect-idempotent (covers R25, R4X-idempotency)

**Given**: no effect `E` in project `p`
**When**: DELETE `/api/projects/p/track-effects/E`
**Then**:
- **status-200**: 200 (NOT 404)
- **body-success**: empty or `{ok:true}`

#### Test: plugin-get-dispatch-success (covers R5, R43, R44)

**Given**: plugin `light_show` registered GET handler for `^/api/projects/(?P<project>[^/]+)/plugins/light_show/scenes$`
**When**: GET `/api/projects/p/plugins/light_show/scenes`
**Then**:
- **status-200**: 200
- **body-plugin-json**: body is the plugin-returned object

#### Test: plugin-handler-exception-500 (covers R48)

**Given**: plugin handler raises `RuntimeError("boom")`
**When**: POST the matching plugin path
**Then**:
- **status-500**: 500
- **code-plugin-error**: `PLUGIN_ERROR`
- **error-includes-exception**: error string contains `"boom"`

#### Test: plugin-named-groups-propagated (covers R45)

**Given**: plugin pattern `.../plugins/x/items/(?P<item_id>\d+)`
**When**: GET `/api/projects/p/plugins/x/items/42`
**Then**:
- **handler-received-groups**: plugin handler sees `path_groups={"item_id":"42"}` (assert via plugin echo endpoint used in test)

#### Test: plugin-no-groups-no-kwarg (covers R45)

**Given**: plugin pattern with no named groups
**When**: request hits it
**Then**:
- **no-kwarg**: `path_groups` NOT in kwargs (back-compat)

#### Test: auth-login-valid-code-303 (covers R7, /auth/login)

**Given**: login code `C` issued for JWT `T`
**When**: GET `/auth/login?code=C&redirect_uri=/home`
**Then**:
- **status-303**: 303
- **location**: `Location: /home`
- **set-cookie**: cookie contains `T`
- **code-consumed**: second GET with same `C` → 401 `INVALID_CODE`

#### Test: auth-logout-clears-cookie (covers exempt)

**Given**: valid session
**When**: POST `/auth/logout`
**Then**:
- **status-200**: 200
- **ok-body**: `{"ok":true}`
- **cookie-cleared**: `Set-Cookie: scenecraft_jwt=; Max-Age=0`

#### Test: oauth-authorize-unknown-service (covers /oauth)

**When**: GET `/api/oauth/not-a-service/authorize`
**Then**:
- **status-404**: 404
- **code**: `UNKNOWN_SERVICE`

#### Test: error-shape-preserved-401 (covers R25, R27)

**Given**: invalid token
**When**: GET any authed endpoint
**Then**:
- **keys-exactly**: body keys are exactly `{error, code}` (NOT `{detail}`)
- **no-fastapi-detail**: assert there is no `detail` key

### Edge Cases

#### Test: bearer-hard-expiry-24h (covers R12, OQ-related)

**Given**: bearer issued >24h ago
**When**: any authed request
**Then**:
- **status-401**: 401
- **no-refresh**: no Set-Cookie refresh attempted

#### Test: concurrent-cookie-refresh-both-valid (covers R11)

**Given**: two parallel GETs with same cookie `T`
**When**: both succeed
**Then**:
- **two-set-cookies**: each response carries its own refreshed cookie
- **both-accepted**: either can be used for a subsequent request

#### Test: url-decoded-project-name (covers R4)

**Given**: project named `my proj` on disk
**When**: GET `/api/projects/my%20proj/keyframes`
**Then**:
- **status-200**: 200
- **handler-sees-decoded**: handler receives project_name `"my proj"`

#### Test: encoded-slash-in-project-404 (covers R4)

**When**: GET `/api/projects/a%2Fb/keyframes`
**Then**:
- **status-404**: 404 (regex `[^/]+` rejects decoded `a/b`)

#### Test: head-missing-empty-404 (covers R41)

**When**: HEAD on non-existent file
**Then**:
- **status-404**: 404
- **body-zero-bytes**: no body (NOT `{error,code}` JSON)

#### Test: internal-broadcast-requires-auth (covers R14-adjacent)

**Given**: auth enabled; no token
**When**: POST `/api/_internal/broadcast`
**Then**:
- **status-401**: 401 (no exempt for this path)

#### Test: range-past-eof-clamped (covers R38)

**Given**: 1000-byte file
**When**: GET with `Range: bytes=99999-`
**Then**:
- **status-206**: 206
- **end-clamped**: `Content-Range` shows `end = 999`
- **content-length-negative-or-zero**: `undefined` behavior; test documents current result

#### Test: missing-content-length-400 (covers R19)

**Given**: POST with no Content-Length header
**When**: body ignored
**Then**:
- **status-400**: 400
- **error-empty-body**: `"Empty body"`

#### Test: options-bypasses-auth (covers R17)

**Given**: auth enabled, no token
**When**: OPTIONS `/api/projects`
**Then**:
- **status-204**: 204 (NOT 401)

#### Test: delete-cookie-refreshes (covers R11)

**Given**: cookie auth
**When**: DELETE on idempotent endpoint
**Then**:
- **set-cookie-present**: refreshed cookie on response

#### Test: no-jwt-in-logs (covers negative-assertion, security hardening)

**Given**: request with JWT
**When**: `_log` output captured for the request lifecycle
**Then**:
- **no-jwt-substring**: no log line contains the full JWT token
- **no-cookie-substring**: no log line contains `scenecraft_jwt=<…>` pattern

#### Test: error-code-catalog-preserved (covers R26)

**Given**: catalog of known `code` strings (see R26)
**When**: trigger each error path across endpoints
**Then**:
- **every-code-used**: every enumerated code string appears in at least one response in the test suite
- **no-new-codes**: scan source for `self._error(..., "<CODE>", ...)` call sites; every string matches the catalog (meta-test)

#### Test: builtin-first-match-wins (covers R1)

**Given**: two builtin routes with overlapping regex
**When**: request hits overlap
**Then**:
- **top-most-wins**: the earlier `re.match` in `do_<M>` dispatches

#### Test: plugin-bad-signature-500 (covers R48)

**Given**: plugin handler signature lacks `**kwargs` but pattern has named groups
**When**: request hits it
**Then**:
- **status-500**: 500
- **plugin-error**: `PLUGIN_ERROR` (TypeError wrapped)

#### Test: handler-returns-unserializable-500 (covers R22)

**Given**: handler returns object containing a `datetime`
**When**: request invokes it
**Then**:
- **status-500**: 500
- **code**: `INTERNAL_ERROR`

#### Test: validation-exception-does-not-affect-response (covers R31)

**Given**: locked-route handler succeeds; `validate_timeline` raises
**When**: request
**Then**:
- **status-200**: 200
- **log-validation-error**: `_log` shows `"Validation error:"`

#### Test: cookie-refresh-failure-graceful (covers R11)

**Given**: `generate_token` raises inside the refresh path
**When**: cookie-authed GET
**Then**:
- **status-200**: 200
- **no-set-cookie**: response has no Set-Cookie (refresh skipped silently)

#### Test: concurrent-cookie-refresh-both-valid (edge)

**Given**: two parallel cookie-authed requests
**When**: both trigger refresh
**Then**:
- **both-200**: both succeed
- **independent-tokens**: each Set-Cookie is independently decodeable

#### Test: all-routes-require-auth-except-exempts (parametric)

**Given**: catalog of every (method, path) except `/auth/login`, `/auth/logout`, `/oauth/callback`
**When**: request without token (auth enabled)
**Then**:
- **status-401**: 401 for all
- **code-unauthorized**: `UNAUTHORIZED`

#### Test: exempt-paths-no-auth-needed (parametric)

**Given**: auth enabled, no token
**When**: request `/auth/login?code=…`, `/auth/logout`, `/oauth/callback?error=x`
**Then**:
- **not-401**: none return 401 for the auth reason

#### Test: every-error-uses-standard-shape (parametric)

**Given**: list of trigger recipes for every `{code}` in the catalog
**When**: each triggered
**Then**:
- **shape-invariant**: response body JSON has keys `{error, code}` (no `detail`, no alternative shape)

#### Test: ws-path-not-dispatched (covers R2)

**When**: GET `/ws/chat/p` (via HTTP, not WS upgrade)
**Then**:
- **not-json-404**: 404 or handshake failure — specifically NOT a `/api/**` 404 JSON (ws_server handles)
- **documented-separately**: cross-reference to ws_server spec

#### Test: head-video-accept-ranges (covers R41)

**Given**: video file
**When**: HEAD on it
**Then**:
- **accept-ranges-bytes**: `Accept-Ranges: bytes`
- **content-length-matches**: file size
- **content-type-video**: starts with `video/`

#### Test: cookie-samesite-lax (covers R11)

**Given**: any cookie-emitting path
**When**: inspect Set-Cookie
**Then**:
- **samesite-lax**: `SameSite=Lax`
- **httponly**: `HttpOnly`
- **path**: `Path=/`

---

## Migration Contract

FastAPI rewrite MUST preserve these behaviors. Each is a non-negotiable refactor invariant, enumerated so reviewers can scan for regressions:

1. **Error body shape `{error, code}`**. FastAPI's default is `{detail: ...}`. Override via a custom `exception_handler` for `HTTPException` AND the request-validation exception handler (422 → 400 with `{code:"BAD_REQUEST", error:"<detail>"}`).
2. **Missing token → 401, not 403**. FastAPI's `HTTPBearer(auto_error=True)` emits 403; MUST be configured to emit 401 via `auto_error=False` + manual raise.
3. **Bearer token takes precedence over cookie**. Dependency must check `Authorization` first, fall back to cookie.
4. **Sliding cookie refresh on authenticated cookie requests only**. Bearer requests MUST NOT receive Set-Cookie.
5. **Cookie name `scenecraft_jwt`, attributes `Path=/; HttpOnly; SameSite=Lax; Max-Age=86400`**. `Secure` optional per deployment.
6. **CORS headers on EVERY response**, including errors. FastAPI's default `CORSMiddleware` does not add CORS to middleware-raised 401s under all configurations — verify. Origin echo + credentials=true when Origin present; wildcard when absent.
7. **No Origin allowlist (preserved XSRF exposure)**. Any Origin is echoed. See [OQ-2](#open-questions).
8. **Exempt paths**: `/auth/login`, `/auth/logout`, `/oauth/callback` bypass auth entirely.
9. **OPTIONS = 204 for any path**, without auth check, with CORS headers.
10. **Empty body → 400 `{code:"BAD_REQUEST", error:"Empty body"}`**. FastAPI's default 422 for missing body must be overridden for every JSON-body endpoint (likely via a dependency that reads raw body and mirrors `_read_json_body`).
11. **Invalid JSON → 400 `{code:"BAD_REQUEST", error:"Invalid JSON: …"}`**. Same override path.
12. **Per-project structural lock on exactly 11 POST routes** (R28). FastAPI dependency must acquire+release around the handler.
13. **Post-mutation `validate_timeline` call on those 11 routes only**, logging + WS broadcast, non-blocking on response.
14. **Plugin REST fallback** for GET/POST on `/api/projects/:p/plugins/:plugin/*`; `path_groups` kwarg propagation for patterns with named groups; 500 `PLUGIN_ERROR` on exception. DELETE/PATCH/PUT currently NOT forwarded (see [OQ-4](#open-questions)).
15. **File serving**: Range 206, ETag 304, If-Modified-Since 304, 65KB chunked reads, path-traversal `startswith` guard (symlink-bypassable — see [OQ-3](#open-questions)). FastAPI's `FileResponse` uses starlette's ranged response; verify it emits the same headers and honors the same chunk size, else use a custom `StreamingResponse`.
16. **HEAD on files-only path → 200 with headers, empty body**. Other HEAD paths → 405 empty body, NOT a JSON error.
17. **DELETE idempotency for the 4 M13 routes**: non-existent id → 200, not 404.
18. **Response `Content-Length` set explicitly** (not `Transfer-Encoding: chunked`) for JSON responses.
19. **Client-disconnect tolerance**: `BrokenPipeError` / `ConnectionResetError` mid-write must be swallowed, not logged as error.
20. **Error-code catalog preserved**: every string that currently appears in a `self._error(status, "CODE", message)` call must be emitted by the corresponding route in the rewrite. Catalog is frozen at migration time.
21. **`/api/_internal/broadcast` behavior**: requires auth (unless behind localhost binding), emits 200 `{ok:true}` on success.
22. **No top-level try/except around handler body in current code**; FastAPI rewrite MAY add one but MUST emit `{code:"INTERNAL_ERROR", error: …}` for unexpected exceptions rather than `{detail}`.

---

## Known divergence from FastAPI defaults

Places where the rewrite must explicitly override FastAPI to preserve current contract:

| FastAPI default | Current behavior | Action |
|---|---|---|
| Error body `{detail}` | `{error, code}` | Register custom `exception_handler(HTTPException)` and `RequestValidationError` handlers |
| Missing `Authorization` → 403 via `HTTPBearer` | 401 `UNAUTHORIZED` | Use `HTTPBearer(auto_error=False)` + manual 401 raise |
| 422 on body validation fail | 400 `BAD_REQUEST` | Override `RequestValidationError` handler |
| Empty body raises 422 | 400 `{error:"Empty body"}` | Read raw body, check length, emit 400 before validation |
| CORS via `CORSMiddleware` with allow_origins list | No allowlist (echo any Origin) | Custom middleware or `allow_origin_regex=".*"` + manual `Vary: Origin` |
| `FileResponse` Range headers | Same but verify `Cache-Control: public, max-age=3600, immutable` | Use `StreamingResponse` with manual headers if needed |
| OPTIONS handled by CORSMiddleware only | Dispatcher returns 204 for any path unconditionally | Custom OPTIONS handler or verify middleware ordering |
| Default JSON encoder | `json.dumps` equivalent; non-serializable values → 500 | Use `ORJSONResponse` or default, ensure encoder behavior matches |
| Pydantic auto-trims extra fields | Current code tolerates extra fields silently | Use `model_config = ConfigDict(extra="allow")` or parse as `dict` |
| Body size unlimited | Unlimited (no cap) | See [OQ-5](#open-questions) |
| `HEAD` auto-implemented for `GET` routes | Only `/api/projects/:p/files/*` has HEAD; other paths → 405 | Explicitly register HEAD only on files path; for others, return 405 |
| Trailing-slash redirect (`redirect_slashes=True`) | Current returns 404 for `/api/projects/` | Set `FastAPI(redirect_slashes=False)` |

---

## Non-Goals

- Replacing any current status code with a "better" one. If current code emits 200 for an idempotent DELETE, rewrite emits 200 too.
- Introducing a central error-code registry (separate refactor; tracked as audit leak #24).
- Fixing the CORS XSRF exposure in this migration. It is documented, tested as-is, and flagged as [OQ-2](#open-questions).
- Fixing the symlink path-traversal bypass ([OQ-3](#open-questions)).
- Applying `@require_paid_plugin_auth` to any endpoint ([OQ-1](#open-questions)).
- Adding a request-body size cap ([OQ-5](#open-questions)).
- Adding bearer-refresh semantics (audit leak #12) — bearer remains 24h hard expiry.
- Refactoring the 11-route structural lock into a general locking strategy. The boundary is frozen.
- Changing the hardcoded plugin-loading bootstrap order.

---

## Open Questions

- **OQ-1** — `@require_paid_plugin_auth` is defined but applied to zero endpoints. Should the FastAPI port (a) delete it as dead code, (b) preserve it as-is and leave it unused, or (c) apply it to specific paid-plugin endpoints (which?)? Blocks rows 57–64.
- **OQ-2** — CORS has no Origin allowlist. Should the port add one? If so, which origins? (Frontend dev + tunnel domain are the known consumers.) Audit leak #5.
- **OQ-3** — Path-traversal guard uses `startswith` on the resolved path but does not re-validate the real target if the final component is a symlink into work_dir that points outside. Accept the current behavior or add `resolve(strict=True)` + `relative_to` check? Audit leak #11.
- **OQ-4** — Plugin REST dispatch only forwards GET and POST. Should DELETE, PATCH, PUT forward too? No current plugin uses them, but a `stem_splitter` or future plugins may.
- **OQ-5** — No body-size cap exists. Should the port add one globally (e.g., 200MB for multipart, 1MB for JSON)? At what status code — 413 Payload Too Large?
- **OQ-6** — Concurrent uploads to the same `pool/upload` destination: no mutex; filesystem last-writer-wins. Acceptable or add per-target lock?
- **OQ-7** — Request carries two `scenecraft_jwt` cookies (malicious or accidental). Which wins? Current: first matching segment in `Cookie:` header (platform-dependent).
- **OQ-8** — Range with `end < start`: current regex accepts, behavior undefined. Reject as 416?
- **OQ-9** — Multiple byte-ranges in one header (`Range: bytes=0-100,200-300`). Should server support multipart/byteranges response or keep current first-range-only?
- **OQ-10** — Very long URLs (>8KB): Python stdlib limits apply in current code. FastAPI's limits differ. Document the operative cap.
- **OQ-11** — `Access-Control-Allow-Headers` currently lacks `X-Scenecraft-API-Key` and `X-Scenecraft-Org` (referenced by paid-plugin decorator). Add them to unblock future paid-plugin routes, or defer until OQ-1 resolves?
- **OQ-12** — On cookie refresh, `generate_token` builds the payload from the DB row — not from the old payload. `last_active_org` claim behavior: does it survive refresh? Likely no. Spec handler behavior.
- **OQ-13** — `Expect: 100-continue`: Python stdlib `BaseHTTPRequestHandler` supports it. Does FastAPI/uvicorn? Behavior for large bodies.
- **OQ-14** — `Transfer-Encoding: chunked` POSTs: `_read_json_body` reads `Content-Length` bytes, which is absent for chunked. Current behavior: reads 0 bytes → 400 Empty body. FastAPI/uvicorn will handle chunked natively. Intentional regression-parity (reject) or bug-fix (accept)?
- **OQ-15** — DELETE semantics of `/workspace-views/:view/delete` when view missing. Idempotent 200 (match M13 DELETE pattern) or 404?
- **OQ-16** — Uncaught exceptions inside a handler: current code has NO try/except at dispatcher level, stdlib default is "500 with empty body". Should the port wrap handlers with a try/except that emits `{code:"INTERNAL_ERROR"}`? If yes, Migration Contract item 22 becomes binding.
- **OQ-17** — Should OPTIONS responses include `Access-Control-Max-Age` (preflight caching)? Current: absent. Adding it is a performance win, not a correctness change.
- **OQ-18** — JWT validates but lacks `sub` claim: `_authenticated_user` becomes `None`. Downstream handlers may attribute actions to `None`. Treat as 401 `MALFORMED_TOKEN` at dispatcher level?

---

## Related Artifacts

- Audit: `agent/reports/audit-2-architectural-deep-dive.md` (§1A units 1–12, §3 leaks #4, #5, #7, #11, #12, #13, #24)
- Existing engine specs NOT to duplicate: `local.fastapi-migration.md` (this spec is the behavioral contract it must satisfy), `local.openapi-tool-codegen.md`, `local.effect-curves-macro-panel.md` (endpoint-level specifics for M13 DELETE routes)
- Scenecraft project specs referenced: `local.auth-jwt-api-keys-double-gate.md`, `local.plugin-host-and-manifest.md`, `local.plugin-api-surface-and-r9a.md`, `local.job-manager-and-ws-events.md`
- Follow-on engine specs (not yet written, same fan-out batch): `engine-file-serving-and-uploads` (narrower companion), `engine-server-bootstrap`, `engine-plugin-loading-lifecycle`

---

## Notes

- This spec is deliberately a **black-box HTTP contract**, not an implementation guide. Internal restructuring (splitting api_server.py into routers, adopting dependency injection, using Pydantic models) is encouraged and orthogonal.
- The ~180 rows here are the minimum. When a specific endpoint's handler spec (e.g. `engine-bounce`, `engine-chat-pipeline`) adds handler-specific failure modes, those become additional rows in **that** spec, not this one.
- `undefined` rows are intentional. They are the highest-signal items during refactor review: resolving them is how the port avoids silent behavior drift. The answer for each OQ should land in this spec (edit in place) before the FastAPI PR merges.
- The spec does NOT enumerate every error-code string individually with a test because the `every-error-uses-standard-shape` parametric test exhaustively verifies the shape, and `error-code-catalog-preserved` verifies the string set. Adding 40 near-duplicate tests would bloat the suite without adding coverage.
