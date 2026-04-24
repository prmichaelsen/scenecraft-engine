# Milestone 16: FastAPI Migration + OpenAPI-Driven Tool Codegen

**Goal**: Replace the hand-rolled 10,320-LOC `api_server.py` with a FastAPI/uvicorn implementation, preserving every route's path, response shape, status code, auth behavior, streaming semantics, and plugin compatibility — then build a codegen that emits Anthropic-compatible chat tool schemas from the resulting `openapi.json`, eliminating ~1000 LOC of hand-written tool dicts in `chat.py`.
**Duration**: ~2 weeks (12 tasks, ~50–70 dev hours)
**Dependencies**: None in this repo. M11 WS server stays independent (not migrated). 897-test suite provides migration confidence.
**Status**: Not Started

---

## Overview

Today the REST API is `http.server.BaseHTTPRequestHandler` with 164 routes in a hand-rolled if/`re.match` ladder. And the 32 chat tools in `chat.py` have their `name`/`description`/`input_schema` hand-authored as Python dicts. These are parallel hand-maintained surfaces over the same DB operations, with no shared contract.

This milestone changes both:

1. **Migration** — FastAPI takes over. Every route gets a Pydantic model, an `operationId`, and `openapi_extra` metadata. `GET /openapi.json` emits a valid OpenAPI 3.1 spec as a free side effect. Error envelopes, CORS, auth, Range streaming, structural mutation locks, and the plugin catch-all are all preserved — hard cut, no feature flag.

2. **Codegen** — `scripts/gen_chat_tools.py` walks the spec, filters operations marked `x-tool: true`, and emits `src/scenecraft/chat_tools_generated.py` containing the `TOOLS: list[dict]` registry. `chat.py` imports from it; the 32 legacy constants are deleted. OpenAPI becomes the single authoring surface for tool schemas; drift is structurally impossible.

Everything is pinned by two specs (proofed 2026-04-24):

- [`local.fastapi-migration`](../specs/local.fastapi-migration.md) — **35 requirements, 60 behavior rows, 5 open questions**. Tasks 57–64 deliver this.
- [`local.openapi-tool-codegen`](../specs/local.openapi-tool-codegen.md) — **30 requirements, 37 behavior rows, 5 open questions**. Tasks 65–68 deliver this.

**TDD ordering** — every task lists the named tests from the specs it covers. The task starts by writing those tests (or asserting the existing fixture's failure), then implements, then verifies all named tests pass. This milestone was planned in `--tddmode`: each task is sliced along a group of failing tests, not along code modules.

---

## Deliverables

### 1. FastAPI Application

- `src/scenecraft/api/app.py` — module-level `app: FastAPI`, `create_app(work_dir, *, enable_docs=True)` factory
- `src/scenecraft/api/deps.py` — auth, `project_dir`, and per-project structural lock dependencies
- `src/scenecraft/api/errors.py` — custom `ApiError` + exception handlers emitting the legacy `{"error": CODE, "message": ...}` envelope
- `src/scenecraft/api/streaming.py` — Range-aware `StreamingResponse` helper for `/files/*`
- `src/scenecraft/api/models/` — Pydantic request/response models per domain
- `src/scenecraft/api/routers/` — routers split by domain (see task breakdown)

### 2. Routes — 164 operations across domain routers

Every route from `api_server.py` reachable at identical path + method. Every operation has `operationId`, `summary`, and response schema. The 32 existing chat tool names are adopted verbatim as `operationId`s.

### 3. Auth + Middleware

- Single `current_user` dependency reading bearer then cookie
- CORS via `CORSMiddleware` preserving legacy allow-origin list
- Per-project structural lock as a `Depends` applied to the 10-route structural set
- Post-structural-mutation timeline validator with WS broadcast
- Plugin POST catch-all registered last (after built-ins)

### 4. Streaming Correctness

- `GET /files/*` + HEAD: `Content-Range`, `Accept-Ranges`, 206 partial content, 416 on invalid range, path-traversal rejection
- `/render-frame`: byte-identical JPEGs vs legacy (verified by fixture)
- Multipart uploads via `python-multipart`

### 5. Cutover

- `api_server.py` **deleted**; no `from scenecraft.api_server` imports remain
- `scenecraft` CLI launches uvicorn
- Full 897-test suite green against new server
- Perf baseline comparison for `/render-frame` and `/files/*` Range

### 6. Chat Tool Codegen

- `scripts/gen_chat_tools.py` — reads `openapi.json`, validates extensions, emits generated module
- FastAPI routes annotated with `openapi_extra={"x-tool": True, "x-tool-description": "...", "x-destructive": True/False}` for the 32 existing tools
- `src/scenecraft/chat_tools_generated.py` — generated; contains `TOOLS`, `OPERATIONS`, `DESTRUCTIVE_TOOLS`
- `chat.py` imports from the generated module; the 32 legacy tool-dict constants are deleted; `_is_destructive` consults `DESTRUCTIVE_TOOLS`

### 7. Contract & CI

- `tests/fixtures/openapi.snapshot.json` — committed spec snapshot
- `tests/fixtures/generated_tools.golden.json` — committed tool golden
- `tests/fixtures/legacy_tool_schemas.json` — pre-migration capture of the 32 tool schemas for the parity test
- `tests/test_openapi_contract.py` — snapshot test
- `tests/test_generated_tools_parity.py` — golden test
- CI `tools-up-to-date` job running `gen_chat_tools.py --check`

---

## Tasks

Twelve tasks total (T57–T68), grouped in two phases.

### Phase A — FastAPI Migration (T57–T64)

| # | Title | Est hrs | Covers (spec R) | Headline tests |
|---|---|---|---|---|
| 57 | FastAPI scaffold + streaming spike | 4–6 | R1–R3, R12, R20–R23, R29–R31 | `file-get-no-range`, `file-get-range-206`, `file-get-range-416`, `file-get-suffix-range-416`, `file-head-metadata-only`, `file-traversal-rejected`, `openapi-valid-3-1`, `swagger-ui-renders` |
| 58 | Auth + CORS + error envelope + validation | 4–6 | R9, R11, R13–R17, R26–R28, R48–R50 | `auth-required-returns-401`, `bearer-auth-succeeds`, `cookie-auth-succeeds`, `invalid-json-returns-400`, `missing-field-returns-400`, `options-preflight-204`, `validation-envelope-legacy-shape`, `auth-login-sets-cookie-and-redirects`, `auth-logout-clears-cookie`, `oauth-callback-success`, `oauth-callback-bad-state`, `unknown-route-404`, `unhandled-exception-500-envelope`, `cors-origin-matches-legacy`, `cors-on-every-response` |
| 59 | Structural lock + timeline validator post-hook | 3–4 | R18, R19, R40, R45, R53 | `structural-lock-serializes`, `structural-lock-is-per-project`, `timeline-validator-runs-after-mutation`, `validator-exception-non-fatal`, `lock-released-on-exception`, `validator-exception-lock-released` |
| 60 | Projects + misc routers | 6–8 | R4, R6–R8 | `get-route-parity` (projects slice), `post-route-parity` (meta/config slice), `deprecated-noops-preserved`, `extra-fields-ignored`, `no-body-post-works` |
| 61 | Keyframes + transitions routers | 6–8 | R4, R6–R8, R18 | `get-route-parity`, `post-route-parity`, `delete-route-parity`, `delete-idempotent-parity`, `structural-lock-*` (via routes) |
| 62 | Audio routers (tracks, clips, effects, curves, send-buses, master-bus, mix-render) | 8 | R4, R6–R8 | `get-route-parity`, `post-route-parity`, `delete-idempotent-parity`, `multipart-upload-parity` |
| 63 | Rendering + files + pool + candidates routers | 8 | R4, R6–R8, R20–R24, R51, R52 | `render-frame-bytes-identical`, `large-upload-streams`, `get-route-parity`, `post-route-parity` |
| 64 | Checkpoints + markers + chat + plugin catch-all + unknown-route handler | 4–6 | R4, R25, R28, R56, R57 | `plugin-route-dispatches`, `plugin-error-500`, `plugin-none-returns-404`, `builtin-beats-plugin-catchall`, `unknown-route-404`, `response-shape-parity-crawl` |

### Phase B — Hard Cutover + Tool Codegen (T65–T68)

| # | Title | Est hrs | Covers (spec R) | Headline tests |
|---|---|---|---|---|
| 65 | Hard cutover — CLI swap, delete `api_server.py`, perf baseline, full suite green | 4–6 | R1, R3, R33–R35, R44 | `cli-starts-uvicorn`, `cli-help-unchanged`, `legacy-server-deleted`, `legacy-test-suite-green`, `render-frame-perf-no-regression`, `files-range-perf-no-regression`, `test-client-replaces-http-server-fixture`, `ws-server-independent`, `graceful-shutdown-during-stream`, `chat-exec-paths-unaffected` |
| 66 | `gen_chat_tools.py` — spec walk, schema derivation, deterministic emit, `--check` | 4–6 | codegen R1–R16, R25–R29 | `happy-path-emits-tool`, `unannotated-route-skipped`, `missing-description-errors`, `name-collision-errors`, `ref-resolved-inline`, `allof-flattened`, `polymorphic-body-errors`, `path-body-collision-path-wins`, `query-params-merged`, `empty-input-schema-ok`, `codegen-deterministic`, `check-mode-detects-drift`, `check-mode-silent-when-fresh`, `module-imports-cleanly`, `anthropic-tool-shape-valid`, `invalid-tool-name-errors`, `empty-tool-set-is-fine`, `enum-preserved`, `default-preserved`, `description-preserved-or-empty`, `tool-name-override-wins`, `unresolvable-ref-errors`, `operation-meta-has-templated-path` |
| 67 | Annotate 32 existing routes with `x-tool`/`x-tool-description`/`x-destructive` | 3–4 | codegen R19, R20, R21 | `chat-tool-operation-ids-match`, `32-legacy-tools-preserved`, `legacy-schemas-preserved`, `destructive-flag-captured`, `non-destructive-default` |
| 68 | Wire chat.py + delete legacy + snapshot + golden + parity + CI | 4–6 | codegen R17, R18, R22–R24, R30 | `chat-imports-generated`, `legacy-constants-deleted`, `openapi-snapshot-matches`, `snapshot-test-flags-drift`, `ci-tools-up-to-date-job`, `incremental-add-minimal-diff` |

**Total estimated:** ~54–72 dev hours. Agent hours likely closer to 15–25 given the mostly-mechanical nature of the migration and the spec's density of pre-written test cases.

---

## Success Criteria

- [ ] All 164 existing routes reachable at identical paths and methods.
- [ ] Every route has a stable `operationId`; all 32 chat tool names are adopted as `operationId`s.
- [ ] Full 897-test suite passes against the new server.
- [ ] `api_server.py` is deleted; `git grep "from scenecraft.api_server"` returns zero results.
- [ ] `/render-frame` returns byte-identical JPEGs to legacy fixture.
- [ ] No performance regression ≥ 10% on `/render-frame` or `/files/*` Range fetches.
- [ ] `GET /openapi.json` returns valid OpenAPI 3.1; `tests/fixtures/openapi.snapshot.json` is committed and enforced.
- [ ] `scripts/gen_chat_tools.py` runs cleanly; `--check` mode passes; running twice yields byte-identical output.
- [ ] `chat.py::TOOLS` is imported from `chat_tools_generated`; the 32 hand-written tool-dict constants are deleted.
- [ ] Generated schemas match pre-migration semantics for all 32 tools (via `tests/fixtures/legacy_tool_schemas.json` parity test).
- [ ] CI `tools-up-to-date` job fails on drift.
- [ ] WebSocket server on port 8891 works unchanged.

---

## Non-Goals (from specs)

- Migrating `ws_server.py` — stays independent on 8891
- Adding new endpoints or behavior changes
- Feature-flag toggle between legacy and FastAPI (**hard cut**)
- Extracting chat tool execution to HTTP round-trip or shared service layer (codegen OQ-1 — deferred)
- MCP bridge refactor to consume `OPERATIONS`
- TypeScript client codegen
- Output-schema generation (Anthropic API doesn't support it)
- Performance tuning beyond parity

---

## Open Questions Inherited from Specs

From `local.fastapi-migration`:
- OQ-1: HEAD on `/render-frame` (default: not supported — matches legacy)
- OQ-2: 422 vs 400 envelope for new post-migration endpoints (default: legacy 400 everywhere)
- OQ-3: Non-`tests/` server-instantiation call sites (deferred to T57 audit)
- OQ-4: Exotic response headers beyond the enumerated set (deferred to T60–T64 router audits)
- OQ-5: Uvicorn vs hypercorn vs daphne (default: uvicorn[standard])

From `local.openapi-tool-codegen`:
- OQ-1: Chat tool execution path — (a) in-process direct, (b) ASGI round-trip, (c) extracted service layer. **Recommendation: (a) + cross-execution parity test** (leans on the fact that `_exec_*` already calls into the same `db.*`/`audio_intelligence.*` modules FastAPI handlers will call). Settle before/during T68.
- OQ-2: Output schemas — deferred
- OQ-3: MCP bridge integration — deferred
- OQ-4: Description authoring tooling — deferred
- OQ-5: Generated file location (`generated/` subpackage vs flat) — defer to T66 implementation call

---

## Task Numbering Note

This milestone uses global task IDs **T57–T68**. `progress.yaml` currently ends at T56 (M13). M14 and M15 work exists in git commit history (`feat(api): M15 mix-render-upload endpoint`, `merge(M15): analyze_master_bus chat tool`, etc.) but is not tracked in `progress.yaml` and appears to use per-milestone internal numbering (`task 7`, `task 8`) rather than the global scheme. M14/M15 progress backfill is a separate concern and out of scope here; T57+ is safe to use.

---

## Related Artifacts

- `agent/specs/local.fastapi-migration.md` — migration spec (35R / 60 behavior rows)
- `agent/specs/local.openapi-tool-codegen.md` — codegen spec (30R / 37 behavior rows)
- `src/scenecraft/api_server.py` — the module being replaced (deleted in T65)
- `src/scenecraft/chat.py` — source of the 32 legacy tool-dict constants (deleted in T68)
- `src/scenecraft/ws_server.py` — unaffected; runs independently on port 8891
- `tests/` — 897 tests across 61 files; primary verification surface
