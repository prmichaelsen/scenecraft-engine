# Audit Report: Scenecraft-Engine Architectural Deep Dive

**Audit**: #2
**Date**: 2026-04-27
**Subject**: Engine-side conceptual unit inventory — core components, responsibilities, boundaries, encapsulation, invariants. Substrate for retroactive `@acp.spec` generation across the engine surface.

**Method**: Eight parallel investigations, one per engine subsystem (REST API, chat+LLM, DB+DAL+migrations, render pipeline, bounce+analysis, providers+SDKs, plugin loading, CLI+admin). Each reported conceptual units with responsibility/public surface/boundary leaks/code pointers. This report synthesizes them.

Companion report `agent/reports/handoff-scenecraft-engine-2026-04-25.md` predates this pass; this audit supersedes it for architecture purposes.

---

## Summary

Scenecraft-engine is a **single Python process** running HTTP + WebSocket + job-manager + plugin host + CLI in-process. Per-project SQLite DBs hold state; global server.db holds auth/spend/users. The architecture is **layered but leaky**: REST handlers reach deep into DAL directly (no service layer), plugins have contribution-point-driven registration but fall back to imperative calls freely, and the provider abstraction (typed `plugin_api.providers`) is implemented only for Replicate — all other external SDKs (Imagen, Veo, Kling, Runway, Anthropic, GenAI) bypass it entirely.

**~80 conceptual units** identified across 8 subsystems. **~25 boundary leaks** of varying severity. Biggest concentration of leaks: **provider surface** (dual patterns, 5 of 7 providers not specced/tracked/instrumented), **persistence layer** (FK gaps, inconsistent cascade semantics, migration-by-column-detection with no version table), and **spend attribution** (only Replicate + Musicful wired up; Imagen/Veo/Kling/Runway/Anthropic/GenAI all silently un-tracked).

**What the engine defines**: a REST API backed by per-project SQLite, a tool-calling LLM chat pipeline with elicitation and disconnect-survival, a content-addressed VCS (spec'd in scenecraft audit-2), a render pipeline orchestrating image/video generation and composition, a WebAudio-aligned analysis layer (frontend renders PCM, backend caches librosa analyses by deterministic hash), a typed plugin system with declarative+imperative contributions, and a CLI with 28 admin/dev commands.

---

## 1. Conceptual Units Catalog

### 1A. REST API Surface (13 units)

| # | Unit | Responsibility | Key code |
|---|---|---|---|
| 1 | **Routing Dispatcher** | Method-keyed URL→handler regex matching across ~150 inline routes | `api_server.py:165–2428` |
| 2 | **Auth Gate** | JWT validation (bearer or cookie) + sliding cookie refresh + exempt paths | `api_server.py:120–161` |
| 3 | **Paid Plugin Gate** | `@require_paid_plugin_auth` decorator — **DEFINED BUT NEVER APPLIED** anywhere | `auth_middleware.py:46–204` |
| 4 | **Project Dir Resolver** | Project name → disk path; missing → 404; no symlink check | `api_server.py:103–113` |
| 5 | **Request Body Parser** | Read Content-Length, decode JSON; empty body → 400 | `api_server.py:~1755` |
| 6 | **Response Serializer** | JSON encode + CORS + cookie refresh | `api_server.py:~1737` |
| 7 | **Error Response** | `{error, code}` shape; ~40 error code strings hardcoded at call sites | scattered |
| 8 | **CORS Handler** | Echo Origin; credentials=true when Origin present; NO allowlist | `api_server.py:~1766` |
| 9 | **File Serving** | Range requests, ETag, cache headers; 65KB chunked reads | `api_server.py:8991–9093` |
| 10 | **Plugin REST Registry** | Method-keyed regex→handler; auto-prefix `/api/projects/:name/plugins/:id/` | `plugin_host.py:456–481` |
| 11 | **Structural Lock Manager** | Per-project threading.Lock for 11 structural routes only; most routes race freely | `api_server.py:740–780` |
| 12 | **Timeline Validator** | Post-structural-mutation chain-integrity check; warnings logged but not blocking | `api_server.py:759–776` |
| 13 | **WebSocket Upgrade** | Separate ws_server.py; connects to same DB but distinct route space | `ws_server.py` |

### 1B. Chat + LLM Integration (10 units)

| # | Unit | Responsibility | Key code |
|---|---|---|---|
| 1 | **Tool Catalog** | 34 built-in tools + plugin contributions + bridge; flat dict list | `chat.py:1429–1464` |
| 2 | **Tool Dispatcher** | 150+-line if-chain; plugin tools (`__` prefix) intercepted before built-ins | `chat.py:4924–5149` |
| 3 | **Destructive Classifier** | 3-layer: allowlist > plugin flag > substring patterns | `chat.py:1516–1530` |
| 4 | **Elicitation Pipeline** | Future dict keyed by elicitation_id; 300s timeout; auto-decline; single-reader WS pattern | `chat.py:1770–1796` |
| 5 | **Streaming Loop** | `_stream_response`; 10-iteration cap; partial-persist on CancelledError | `chat.py:5309–5582` |
| 6 | **Chat History** | SQLite `chat_messages` table; 50-message window; JSON blocks for tool_use | `chat.py:27–100` |
| 7 | **System Prompt Builder** | Dynamic per-call; injects fps/resolution/counts/title from project.db | `chat.py:106–225` |
| 8 | **MCP Bridge** | Lazy OAuth connect to Remember; SSE over Bearer; 10s+15s timeouts; degraded-mode = built-ins only | `mcp_bridge.py:42–215` |
| 9 | **WS Connection Handler** | Single-reader pattern; routes elicitation responses to waiting futures; current_stream task cancellation | `chat.py:5199–5307` |
| 10 | **Interrupt Path** | New-message-while-streaming cancels current task; partial message persisted with `interrupted:true` | `chat.py:5525–5572` |

### 1C. DB Schema + DAL + Migrations (12 units + 50+ tables)

| # | Unit | Responsibility | Key code |
|---|---|---|---|
| 1 | **Connection Pool** | Thread-local; memoized per `(db_path, thread_id)`; WAL mode; 60s busy timeout; check_same_thread=False | `db.py:56–79` |
| 2 | **Schema Bootstrap** | `_ensure_schema()`; CREATE TABLE IF NOT EXISTS for 50+ tables | `db.py:148–943` |
| 3 | **Migration Framework** | Additive ALTER TABLE guarded by `PRAGMA table_info` column-existence checks; **no schema_migrations version table** | `db.py:143–145, 940–1010` |
| 4 | **Transaction Context Manager** | `transaction(project_dir)` yields conn; user commits or rolls back on exception | `db.py:86–95` |
| 5 | **Retry-on-locked Helper** | `_retry_on_locked(fn, max_retries=5, delay=0.2)` — 5 attempts with linear backoff (0.2, 0.4, 0.6, 0.8s); matches on `"locked"` substring only | `db.py:27–36` |
| 6 | **JSON-on-Read Pattern** | ~15 JSON columns parsed via `json.loads()` in `_row_to_*` mappers | scattered |
| 7 | **Soft-delete vs Hard-delete** | keyframes/transitions/audio_clips soft; audio_tracks hard with cascade; pool_segments hard with no cascade | scattered |
| 8 | **Undo/Redo System** | Trigger-populated `undo_log`/`redo_log`; explicit `undo_group` boundaries | `db.py` |
| 9 | **Sparse JSON Merge-Patch** | RFC 7396 merge-patch in DAL for `params_json` (light show scenes); raw UPDATE bypasses | `db.py:4690–4703` |
| 10 | **Deferred FK** | `isolation_stems` DEFERRABLE INITIALLY DEFERRED — for undo replay row-order independence | `db.py:388–389` |
| 11 | **Seed Defaults** | 4 default send buses created if empty | `db.py:116–133` |
| 12 | **Analysis Cache Tables** | dsp/mix/description runs + datapoints/sections/scalars — 3-tuple or 5-tuple cache keys, UNIQUE constraint | `db.py` |

**50+ tables** across: timeline entities, candidates, effects, audio system, analysis caches (dsp, mix, description, bounce), light show (6 tables), transcribe (2 tables), generation sidecars (music, foley), undo/redo.

### 1D. Render Pipeline (10 units)

| # | Unit | Responsibility | Key code |
|---|---|---|---|
| 1 | **Keyframe Generation (new)** | Thread-based; `start_keyframe_generation`; Imagen via GoogleVideoClient | `chat_generation.py:45–322` |
| 2 | **Transition Generation (new)** | Thread-based; `start_transition_generation`; Veo via GoogleVideoClient; multi-slot chaining | `chat_generation.py` |
| 3 | **Keyframe Pipeline (legacy)** | `narrative.py:generate_keyframe_candidates`; CLI-invoked; grid contact sheets; parallel | `narrative.py` |
| 4 | **Transition Pipeline (legacy)** | `narrative.py:generate_transition_candidates`; CLI-invoked; slot chaining | `narrative.py` |
| 5 | **Provider Bridge** | GoogleVideoClient for Imagen + Veo; Runway fallback for Veo 3.1 | `render/google_video.py` |
| 6 | **Schedule Builder** | `build_schedule`; segment dedup; overlay loading; effect event rules; transform curve parse | `render/schedule.py` |
| 7 | **Per-Frame Compositor** | `render_frame_at`; time-remap, transform, color grading, blend, effects | `render/compositor.py` |
| 8 | **Transform Application** | `_apply_transform`; Z-scale then X/Y offset per frame; `cv2.resize` + `cv2.warpAffine` | `render/compositor.py:~280` |
| 9 | **Final Assembly** | `assemble_final`; frame loop + cv2.VideoWriter + ffmpeg audio mux | `render/narrative.py` |
| 10 | **Cache Invalidation** | `invalidate_frames_for_mutation`; drops frame_cache + fragment_cache; non-fatal | `render/cache_invalidation.py` |

### 1E. Bounce + Analysis (6 units)

| # | Unit | Responsibility | Key code |
|---|---|---|---|
| 1 | **Bounce Handler** | `_exec_bounce_audio`; WS request_id round-trip; cache by composite_hash (SHA-256); WAV validate + persist | `chat.py:3422` |
| 2 | **Analysis Handler** | `_exec_analyze_master_bus`; WS request_id round-trip; librosa + pyloudnorm; 5-tuple cache key | `chat.py:3036` |
| 3 | **DSP Generator** | `generate_dsp`; synchronous librosa (onsets, RMS, vocal_presence, tempo, centroid); 3-tuple cache key | `chat.py` |
| 4 | **Narrative Descriptions** | `generate_descriptions`; Gemini chunked; structured JSON output; 3-tuple cache key | `chat.py` |
| 5 | **Waveform Peaks** | `/api/projects/:name/pool/:seg_id/peaks`; float16; ffmpeg streaming decode; stat-based cache key | `api_server.py` |
| 6 | **Upload Handlers** | bounce-upload + mix-render-upload multipart; WAV header validation; delete on fail | `api_server.py:5180+` |

### 1F. Providers + External SDKs (9 units)

| # | Unit | Responsibility | Typed/Legacy |
|---|---|---|---|
| 1 | **Replicate Provider** | Typed facade; run_prediction, attach_polling, get_balance; spend_ledger integrated | **Typed** (M18) |
| 2 | **Musicful (call_service shim)** | Legacy `SERVICE_REGISTRY` dict; urllib/httpx; plugin writes spend separately | **Legacy** (M16) |
| 3 | **GoogleVideoClient (Imagen)** | Direct SDK; ADC or API key; `_retry_on_429` helper; NO spend tracking | **Direct** |
| 4 | **GoogleVideoClient (Veo)** | Direct SDK; infinite-retry backoff (60s wait between cycles); NO spend tracking | **Direct** |
| 5 | **KlingClient** | Direct urllib (no SDK); Replicate API Bearer; runs on Replicate infra but charges upstream account; NO spend tracking | **Direct** |
| 6 | **RunwayVideoClient** | Direct httpx; RUNWAY_API_KEY; NO spend tracking | **Direct** |
| 7 | **Anthropic AsyncAnthropic** | Direct SDK via `ai/provider.py`; chat.py streaming; NO spend tracking | **Direct** |
| 8 | **Google GenAI** | Direct SDK via `audio_intelligence.py`; narrative descriptions; NO spend tracking | **Direct** |
| 9 | **Spend Ledger Writer** | `plugin_api.record_spend()` single-write path; unit-agnostic; M17 TODO for process-boundary trust | **Core** |

### 1G. Plugin Loading + Activation (8 units)

| # | Unit | Responsibility | Key code |
|---|---|---|---|
| 1 | **Plugin Discovery** | **Hardcoded imports** in api_server.py:~495 and mcp_server.py:63; no filesystem scan | scattered |
| 2 | **Manifest Loading** | Parse plugin.yaml → typed PluginManifest; metadata-only; failure logged + continues | `plugin_host.py:200–215` |
| 3 | **Plugin Activation** | Call plugin.activate(plugin_api, context); **EXCEPTIONS NOT CAUGHT** → propagates, blocks boot | `plugin_host.py:165–228` |
| 4 | **Contribution Registration** | Two paths: declarative via `register_declared` reading manifest; imperative via direct calls | `plugin_host.py:230–329` |
| 5 | **REST Dispatch** | `PluginHost.dispatch_rest(method, path)` regex match; path_groups kwargs for handlers | `plugin_host.py:456–481` |
| 6 | **Shutdown** | **NO `deactivate_all` hook**; `server.shutdown()` just closes socket; plugin daemons leak | missing |
| 7 | **Disposable Pattern** | VSCode-style LIFO disposal on `deactivate(name)`; errors caught in dispose loop | `plugin_host.py:357–367` |
| 8 | **Migrations API** | **DOES NOT EXIST**. Sidecar tables are auto-created by `_ensure_schema`; no `register_migration` | missing |

### 1H. CLI + Admin Tooling (7 units + 28 commands)

| # | Unit | Responsibility | Key code |
|---|---|---|---|
| 1 | **Entry Point** | `scenecraft` (Click-based); `beatlab` / `scenecraft-cli` aliases referenced in docs but not registered | `cli.py` |
| 2 | **Auth Command Group** | `init`, `token`, `org`, `user`, `session`, `auth keys` — admin bootstrap + key mgmt | `vcs/cli.py` |
| 3 | **Server Command** | `scenecraft server --port 8890 --host 0.0.0.0 --no-auth` | `cli.py` |
| 4 | **Resolve Integration** | `status`, `inject`, `render`, `pipeline` — DaVinci Resolve gRPC client | `cli.py` |
| 5 | **Beat/Analysis Commands** | `analyze`, `run`, `render`, `make-patch`, `candidates`, `select`, `split-sections` — work-dir caching | `cli.py` |
| 6 | **Narrative Commands** | `narrative assemble`, `crossfade` — final video synthesis | `cli.py` |
| 7 | **Audio Intelligence** | `audio-transcribe`, `audio-intelligence`, `audio-intelligence-multimodel`, `effects` — plugin-adjacent CLI | `cli.py` |

---

## 2. Key Invariants

| ID | Invariant | Enforcement |
|---|---|---|
| **R9a** | Plugins never import `scenecraft.db`; access via `plugin_api` | Convention; no runtime check. Allowlist: `generate_foley/_set_derived_from` (TODO cleanup) |
| **Frontend = PCM truth** | No backend audio synthesis; bounce + analyze only consume frontend-uploaded WAVs | Architectural — verified by audit; no synthesis path found |
| **Composite hash determinism** | Bounce/analysis cache keys via SHA-256 over mix_graph_hash + selection + format | Deterministic-by-construction |
| **Generation jobs survive disconnect** | Daemon threads persist; JobManager in-memory; client re-polls | Architectural |
| **Single PluginHost per process** | Class-level state; hardcoded "exactly one" in docstring | Convention |
| **Connection per (project, thread)** | `get_db` memoized by `f"{db_path}:{thread.ident}"` | Explicit |
| **Migration additive-only** | `PRAGMA table_info` + ALTER TABLE ADD COLUMN; no DROP/RENAME | Convention |
| **Undo DEFERRED FK** | `isolation_stems` FK deferred to allow undo replay row-order independence | Explicit SQL |
| **Tool loop cap = 10** | `_stream_response` for loop; silent exit on cap hit | Hardcoded |
| **Chat history window = 50** | `_get_messages` limit; older turns pruned from Claude's view | Hardcoded |
| **Elicitation timeout = 300s** | `_recv_elicitation_response`; auto-decline on timeout | Hardcoded |

---

## 3. Boundary Leaks (ranked by severity)

| # | Leak | Severity | Location |
|---|---|---|---|
| 1 | **Provider spend un-tracked for 6 of 7 providers** — Imagen/Veo/Kling/Runway/Anthropic/GenAI bypass `record_spend` entirely | CRITICAL | `render/google_video.py`, `ai/provider.py`, `chat_generation.py` |
| 2 | **Plugin `activate()` exceptions are fatal** — uncaught, propagate, engine never starts. Conflicts with atomic-activation decision (OQ-4 in scenecraft specs which assumed try/except) | CRITICAL | `plugin_host.py:165–228` |
| 3 | **No shutdown hook for plugin deactivation** — daemon threads + file handles leak every engine restart | HIGH | missing |
| 4 | **`@require_paid_plugin_auth` decorator defined but never applied** to any endpoint — paid plugin auth not actually enforced | HIGH | `auth_middleware.py` |
| 5 | **CORS allows any origin with credentials** — no allowlist; XSRF-exposed | HIGH | `api_server.py:~1766` |
| 6 | **Dual generation paths** — `chat_generation.py` (new) and `narrative.py` (legacy) both call GoogleVideoClient directly, bypass plugin_api.providers | HIGH | both files |
| 7 | **Only 11 routes hold per-project locks**; most REST endpoints race freely on concurrent writes to same project | HIGH | `api_server.py:740–780` |
| 8 | **No FK constraints on transitions.from_kf / to_kf** — stale references possible | MEDIUM | `db.py` |
| 9 | **`display_order` has no UNIQUE** — duplicates possible if reorder DAL bypassed | MEDIUM | `db.py` |
| 10 | **Hard `delete_pool_segment` doesn't cascade junction cleanup** — tr_candidates / audio_candidates orphan | MEDIUM | `db.py:2140` |
| 11 | **Path traversal check doesn't resolve symlinks** — `startswith` on unresolved path is bypassable | MEDIUM | `api_server.py:8996` |
| 12 | **JWT sliding expiration is cookie-only** — bearer tokens hard-expire at 24h with no refresh path | MEDIUM | `vcs/auth.py:151–157` |
| 13 | **Pre-task-130 plugin handlers without `**kwargs` crash** if route has named groups | MEDIUM | `plugin_host.py:477–479` |
| 14 | **Veo has infinite-retry on rate limit** (60s wait between cycles) — can stall forever | MEDIUM | `render/google_video.py` |
| 15 | **Sparse JSON merge-patch is DAL-only** — raw UPDATE can overwrite entire JSON object | MEDIUM | `db.py:4690–4703` |
| 16 | **No schema_migrations version table** — migrations tracked by column-existence only | MEDIUM | `db.py` |
| 17 | **Nullable track_id migration incomplete** — legacy DBs may still have NOT NULL; PRAGMA table_info doesn't validate constraint state | MEDIUM | `db.py` |
| 18 | **No `register_migration` API** — contradicts scenecraft plugin-host spec which describes it | MEDIUM | missing |
| 19 | **Tool progress broadcasts to ALL WS clients**, not just chat session — leaks to unrelated sessions | LOW | `ws_server.py:96–105` |
| 20 | **Plugin tool names silently shadow built-ins** — no conflict warning | LOW | `chat.py:4945 vs 4962` |
| 21 | **Monolithic `_execute_tool` switch** — 150+ line if-chain, scales poorly, no dispatch table | LOW | `chat.py:4924–5149` |
| 22 | **Duration drift tolerance is spec'd (100ms) but not enforced** for bounce uploads | LOW | `chat.py:3422` |
| 23 | **Peak cache staleness if external file edits bypass mtime update** (copy-in-place) | LOW | `api_server.py` |
| 24 | **~40 error code strings hardcoded at REST call sites** — no central registry | LOW | scattered |
| 25 | **`_execute_readonly_sql` has redundant URI+authorizer guards** — belt-and-suspenders, unclear why both | LOW | `chat.py:1829–1833` |

---

## 4. Engine Process Architecture

```
python -m scenecraft server --port 8890
├─ cli.py:main → server() command
├─ resolve_work_dir() → Path
├─ run_server(host, port, work_dir, no_auth)
│   ├─ make_handler(work_dir, no_auth) → SceneCraftHandler class
│   ├─ ThreadedHTTPServer((host, port), handler)
│   ├─ start_ws_server(host, ws_port+1, work_dir)  ← separate thread
│   ├─ PLUGIN HOST BOOTSTRAP (hardcoded order)
│   │   ├─ PluginHost.register(isolate_vocals)
│   │   ├─ PluginHost.register(transcribe)
│   │   ├─ PluginHost.register(generate_music)
│   │   └─ PluginHost.register(light_show)
│   │   (NOTE: generate_foley NOT hardcoded — may be imported elsewhere or missing)
│   └─ server.serve_forever()
│       ├─ HTTP request threads (ThreadingMixIn)
│       │   └─ do_POST → _authenticate → structural_lock? → handler → _json_response
│       ├─ WebSocket threads (/ws/chat/{project}, /ws/jobs, /ws/preview-stream/*)
│       │   └─ handle_chat_connection → _stream_response → Claude streaming + tool loop
│       └─ Daemon threads (plugin-spawned generation jobs)
│           └─ job_manager.update_progress → broadcast to all WS clients
```

---

## 5. Proposed Engine Spec Targets (~18 specs)

Each row = one feature area to fan out as a `@acp.spec` worktree. Scoped narrow for proofability. `undefined` rows expected.

### Core surfaces (8)

| # | Spec target | Primary sources |
|---|---|---|
| 1 | **engine-rest-api-dispatcher** (auth, CORS, routing, locking, error shapes) | api_server.py, auth_middleware.py |
| 2 | **engine-db-schema-core-entities** (keyframes, transitions, audio_clips, audio_tracks, audio_candidates, tr_candidates, audio_clip_links) | db.py |
| 3 | **engine-db-effects-and-curves** (track_effects, effect_curves, sends, buses, master bus model) | db.py |
| 4 | **engine-db-analysis-caches** (dsp_*, mix_*, audio_description_*, audio_bounces) | db.py |
| 5 | **engine-db-undo-redo** (undo_log, redo_log, undo_groups, undo_state, trigger-based capture) | db.py |
| 6 | **engine-connection-and-transactions** (get_db, pool, WAL, retry-on-locked, transaction ctx mgr) | db.py |
| 7 | **engine-migrations-framework** (PRAGMA table_info pattern, plugin sidecar auto-creation, version-tracking gap) | db.py |
| 8 | **engine-file-serving-and-uploads** (pool/files range requests, bounce-upload, mix-render-upload, peaks endpoint) | api_server.py |

### Generation + providers (4)

| # | Spec target | Primary sources |
|---|---|---|
| 9 | **engine-render-pipeline** (schedule build, per-frame composition, transform curves, final assembly + ffmpeg mux) | render/*.py |
| 10 | **engine-generation-pipelines** (keyframe + transition generation, chat_generation.py vs narrative.py duplication, multi-slot chaining) | chat_generation.py, narrative.py |
| 11 | **engine-providers-typed-and-legacy** (Replicate typed, Musicful call_service shim, direct-SDK Imagen/Veo/Kling/Runway/Anthropic/GenAI) | plugin_api/providers, render/, ai/ |
| 12 | **engine-cache-invalidation** (frame_cache, fragment_cache, schedule rebuild signals, non-fatal policy) | render/cache_invalidation.py |

### Chat + jobs (2)

| # | Spec target | Primary sources |
|---|---|---|
| 13 | **engine-chat-pipeline** (tool catalog, dispatcher, destructive gate, elicitation, streaming, history, interrupt) | chat.py |
| 14 | **engine-mcp-bridge** (OAuth Remember, lazy connect, degraded mode, tool routing prefix) | mcp_bridge.py |

### Auxiliary (4)

| # | Spec target | Primary sources |
|---|---|---|
| 15 | **engine-plugin-loading-lifecycle** (discovery, activation, shutdown gap, disposable LIFO, migration gap) | plugin_host.py |
| 16 | **engine-cli-admin-commands** (28 commands across init/token/org/user/session/auth/resolve/narrative/beat) | cli.py, vcs/cli.py |
| 17 | **engine-server-bootstrap** (run_server, work_dir resolve, port binding, WS thread, plugin activation order) | api_server.py, cli.py |
| 18 | **engine-analysis-handlers** (bounce, analyze_master_bus, generate_dsp, generate_descriptions, request_id WS round-trip) | chat.py |

**Not in target list** (already covered by scenecraft audit-2 specs):
- Plugin host API + manifest schema (in scenecraft: `local.plugin-host-and-manifest.md`)
- plugin_api surface + R9a (in scenecraft: `local.plugin-api-surface-and-r9a.md`)
- Replicate provider typed (in scenecraft: `local.replicate-provider.md`)
- VCS object store + refs + commits (in scenecraft: `local.vcs-object-store-commits-refs.md`)
- JWT + API keys + double-gate (in scenecraft: `local.auth-jwt-api-keys-double-gate.md`)
- Pool segments + variant_kind (in scenecraft: `local.pool-segments-and-variant-kind.md`)
- Job manager + WS events (in scenecraft: `local.job-manager-and-ws-events.md`)
- Chat tool dispatch + elicitation (in scenecraft: `local.chat-tool-dispatch-and-elicitation.md`) — engine spec (#13 above) is a superset covering full engine-side pipeline

**Overlap decision**: Engine specs #13 (chat pipeline) and the scenecraft chat-tool-dispatch spec overlap. Keep both: scenecraft one is about the dispatch contract (how tools behave, shape of events); engine one is about the pipeline internals (streaming loop, history, MCP bridge integration, interrupt path). Different granularities, different concerns.

**Already-specced in engine project** (do NOT re-spec): local.effect-curves-macro-panel, local.fastapi-migration, local.openapi-tool-codegen.

---

## 6. Recommendations

1. **Fix the plugin activation policy first.** Current code has NO try/except around `activate()`, so one failing plugin takes down the engine. This contradicts the atomic-activation decision we made in scenecraft OQ resolution. Align the code to the spec: wrap activation in try/except, LIFO-dispose on failure, log with plugin name.
2. **Add `deactivate_all()` hook to server shutdown.** Trivial fix, closes the daemon-thread-leak boundary leak.
3. **Spec the provider surface as one unified contract**, not 7 divergent patterns. Migrate Imagen/Veo/Kling/Runway to `plugin_api.providers` namespace post-spec. Wire spend_ledger for all 7 providers.
4. **Add `schema_migrations` version table** so migration state can be queried, not inferred from column existence. Required before any plugin `register_migration` API can exist.
5. **Apply `@require_paid_plugin_auth`** to the endpoints that actually require it, OR remove the decorator as dead code. Either way, fix the "defined but never used" gap.
6. **CORS allowlist.** Current "any origin with credentials" is a real XSRF exposure. Add allowlist + validate Origin header.
7. **Fan out the 18 specs in parallel** (matches the scenecraft pattern).
8. **Resolve OQs interactively** using the same block-concept pattern that closed ~110 OQs on the scenecraft side.

---

**Audit complete**: report saved at `agent/reports/audit-2-architectural-deep-dive.md`.
**Next**: fan out 18 parallel `@acp.spec` agents using §5 as the target list.
