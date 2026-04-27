# Engine OQ Resolution Decisions — 2026-04-27

Authoritative list of decisions from the engine-side spec OQ resolution session. Input to spec-patching agents.

Scope: 18 engine specs under `/home/prmichaelsen/.acp/projects/scenecraft-engine/agent/specs/local.engine-*.md`. Resolves ~144 OQs using the same block-concept pattern as the scenecraft-side pass.

Default approach across all blocks (per user directive): **spec the target-ideal state**. Current code that doesn't match becomes "transitional behavior" in the spec — documented, not guessed, but not codified as the eventual contract.

Each resolved OQ is moved from `## Open Questions` → `### Resolved` subsection with the decision text. Matching `undefined` Behavior Table rows flip to concrete expected behavior. New tests added under `### Base Cases` or `### Edge Cases`. Deferred OQs stay in `## Open Questions` with `**Deferred**: <reason>` annotation.

---

## Cross-cutting invariants

### INV-1 (carried over from scenecraft pass): Single-writer per (user, project)

Backend accepts concurrent operations from different users on the same project, and from the same user across different projects. Concurrent operations from the same user on the same project are **undefined** and out of scope. No per-project mutex enforcement — the VCS session model makes each user's working copy its own island. Negative-assertion tests in every affected spec: "no internal lock is held across this API call."

Affects: cache-invalidation OQ-5, connection-and-transactions OQ-1/4/7 (partial), db-analysis-caches OQ-5, db-schema-core-entities OQ-5, db-undo-redo OQ-7, file-serving OQ-6, generation-pipelines OQ-5, providers-typed-and-legacy OQ-6, rest-api-dispatcher OQ-6, render-pipeline OQ-1.

### INV-7 (NEW): Per-working-copy frame cache and fragment cache

Frame caches and fragment caches are keyed by `session_id` (or `working_copy_db_path`), NOT by `project_dir`. Each user's working copy has its own isolated cache. Cache invalidation likewise operates on `(working_copy, ranges)` not `(project_dir, ranges)`. Cache partitioning follows the same working-copy boundary as DB writes (INV-1).

Exception: **peaks cache** (`audio_staging/.peaks/`) stays project-scoped — peaks are content-addressed via file stat + pool_path, so they're genuinely shareable across working copies.

Affects: `engine-cache-invalidation.md` (primary), `engine-render-pipeline.md`, `engine-file-serving-and-uploads.md` (peaks-shared callout), plus scenecraft-side `vcs-object-store-commits-refs.md` as a downstream consequence.

Implementation follow-up (not blocking spec close): future milestone migrates `frame_cache.global_cache` + `fragment_cache.global_fragment_cache` to working-copy-keyed storage.

### INV-8 (NEW): Specs capture target-ideal; current code marked transitional

Where the target-ideal diverges from current code, spec captures **both**: Requirements encode the target, a dedicated "Transitional Behavior" section documents what ships today. Behavior Table rows may have both a target row and a (current) row where useful for the FastAPI refactor. Tests in Base/Edge cover target; transitional tests live in a separate subsection where they regression-lock the today-behavior until the refactor lands.

Applies to: migrations-framework, plugin-loading-lifecycle, providers-typed-and-legacy, chat-pipeline (event naming), rest-api-dispatcher (error shape + Range compliance), and all specs where target > current.

---

## Per-spec decisions

### analysis-handlers

- **OQ-1 (late upload after timeout — orphan WAV)**: **fix** — startup sweep deletes WAVs under `pool/bounces/` and `pool/mixes/` that have no corresponding DB row. Add tests.
- **OQ-2 (composite_hash cache hit but file missing)**: **fix** — stat-check file on cache hit; treat missing as cache miss, re-run. Add test.
- **OQ-3 (librosa raises mid-analysis in `analyze_master_bus` with no inner try/except)**: **fix** — wrap each individual analysis in try/except; partial-run rollback on any failure, row deleted, error surfaced. Add tests per analysis (rms, peak, clipping_detect).
- **OQ-4 (Gemini rate limit mid-chunk)**: **fix** — fail the whole description run on rate limit; no partial description rows persisted. Add test.
- **OQ-5 (concurrent peaks request same clip)**: **fix** — file-lock write-side via atomic rename (`write_bytes` to `.tmp`, atomic `rename` to final). Readers see pre-rename full file or new full file, never partial.
- **OQ-6 (WS closes mid-wait)**: **fix** — distinguish WS-close from timeout via explicit cancellation event; emit `core__chat__tool_result` with `isError:true, reason:"client_disconnected"` rather than silent timeout.
- **OQ-7 (source file mutating during analysis)**: **codify** — analyze on-disk snapshot at time of dispatch; mutation during run is undefined but tolerated (pool segments are content-addressed immutable; mutation rare). Add negative-assertion test: "no source-file fsevent watcher."

### cache-invalidation

- **OQ-1 (invalidate during active scrub — flicker?)**: **codify** — brief re-render at next-visible-frame; no UI flicker, just cache miss triggering fresh render. Preview worker re-primes on-demand.
- **OQ-2 (BG requeue silent failure — cache/DB drift)**: **fix** — on BG requeue raise, fall back to wholesale `invalidate_project` on coordinator. Return tuple gains third field `coordinator_fallback: bool`.
- **OQ-3 (non-overlapping ranges during scrub)**: **codify** — no-op on active fragment. Add negative-assertion test.
- **OQ-4 (wholesale invalidate during active render)**: **codify** — render finishes with stale data (snapshot-at-start semantics). Contract R_N: "`assemble_final` holds schedule from t=0; coordinator signals don't abort in-flight renders."
- **OQ-5 (concurrent invalidations)**: close per INV-1.
- **OQ-6 (negative time values)**: **fix** — clip to `max(0, t)` at function boundary; caches never see negative times.
- **OQ-7 (unknown project_dir)**: **codify** — silent no-op (matches current). Add assertion.
- **OQ-8 (very large range lists)**: **codify** — accept without threshold; linear iteration; caller responsibility for batch size. Add edge test at 1000 ranges.
- **INV-7**: function signature migrates to `invalidate_frames_for_mutation(working_copy, ranges)` as target; current `project_dir` signature transitional.

### chat-pipeline

- **OQ-1 (tool_progress broadcast to off-session WS clients — audit leak #19)**: **fix** — scope events to the originating chat session's WS. Closes leak.
- **OQ-2 (plugin tool name collision with built-in — audit leak #20)**: **fix** — warn on registration (log + dev-only console). Precedence already decided in scenecraft spec (plugin wins).
- **OQ-3 (history window filled with tool-use blocks)**: **fix** — count conversational turns toward 50-msg cap; tool_use/tool_result content blocks ride along with their parent turn but don't consume a slot.
- **OQ-4 (elicitation_waiters leak on client disconnect)**: **fix** — on WS disconnect, cancel all pending futures for that session + purge from waiters dict.
- **OQ-5 (MCP bridge connect succeeds after initial failure)**: **fix** — on successful late-connect, emit `core__chat__mcp_tools_ready` event. Client surfaces a subtle toast.
- **OQ-6 (Anthropic SDK not pinned)**: **fix** — pin in pyproject.toml + compat-check at import.
- **INV-4 divergence (bare event names vs `core__chat__*`)**: target state = `core__*` namespacing per scenecraft INV-4; current bare event names captured in Transitional Behavior section. FastAPI refactor must rename at cutover.

### cli-admin-commands

- **OQ-1 (org create duplicate)**: **fix** — catch SQLite UNIQUE, exit 1 with "org already exists — use `org update` for metadata changes." No `--force`.
- **OQ-2 (user add duplicate)**: **fix** — same pattern as OQ-1. Org membership idempotent via `--ensure-org-membership` implicit.
- **OQ-3 (prune during active server)**: **fix** — advisory `flock` on `.scenecraft/admin.lock`. Mutating CLI commands (prune, keys issue/revoke, user add, org create) acquire; fail fast with "another admin operation in progress" if held. Server holds read lock only on sessions.db (no blocking).
- **OQ-4 (cross-user CLI invocation)**: **codify** — formalize as R_N: "CLI relies on OS filesystem ACLs; no UID/ACL check; DB-open permission errors wrapped with friendly 'cannot access <path>: <errno>' message."
- **OQ-5 (concurrent CLI invocations)**: close per OQ-3 advisory lock. Add retry-with-backoff on lock-acquire (3 attempts, 1s each) before surfacing.
- **OQ-6 (missing commands)**: **defer to separate milestones** — `scenecraft backup`, `restore`, `list-projects`, `gc`, `audit`, `export-project` flagged as target command surface. Implementation paced per milestone. `reset-password` belongs in frontend, not CLI — explicit non-CLI.

### connection-and-transactions

- **OQ-1 (concurrent writes on shared conn)**: close per INV-1. Add negative-assertion test: "DAL callers MUST NOT share a conn across threads; the pool's thread-ident keying is the enforcement mechanism."
- **OQ-2 (connections abandoned by dead threads)**: **fix** — switch `_connections` to `threading.local()`-based storage; entries GC with their thread. No manual close required.
- **OQ-3 (retry exhaustion contract)**: **codify** — `_retry_on_locked` is the final retry budget (5 × linear backoff 0.2, 0.4, 0.6, 0.8s). Combined with 60s SQLite busy_timeout, effective ceiling ≈ 5 × (60s + 0.8s) ≈ 5 min worst case. Contract: callers treat lock errors as fatal; no caller-side retry loops.
- **OQ-4 (close_db while other thread holds conn)**: close per INV-1 + threading.local fix from OQ-2 (no cross-thread sharing means this can't happen).
- **OQ-5 (close_db prefix match too loose)**: **fix** — tighten to `k.startswith(f"{db_path}:")`.
- **OQ-6 (`_retry_on_locked` substring matcher)**: **fix** — match on `sqlite3.OperationalError` with `sqlite_errorcode in (SQLITE_BUSY, SQLITE_LOCKED)`; substring match transitional. Locale-independent.
- **OQ-7 (`transaction` accepts only project_dir)**: **fix** — accept optional `db_path` parameter for session working-copy DBs.
- **OQ-8 (PRAGMA order)**: **fix** — defer `foreign_keys=ON` until after `_ensure_schema` completes. Prevents FK errors during migration ALTER chains. Other PRAGMAs (WAL, NORMAL, busy_timeout) applied pre-migration as today.

### db-analysis-caches

- **OQ-1 (librosa downgrade orphans)**: **fix** — `scenecraft cache prune --analyzer-version <old>` CLI command (part of the new `cache` command group in block 8 CLI additions).
- **OQ-2 (hash collision policy — inconsistent SHA-256 vs SHA-1-64)**: **fix** — standardize on SHA-256 full-digest everywhere. Peaks cache migrates to SHA-256; old SHA-1-64 entries invalidated via one-shot migration on first access.
- **OQ-3 (partial run rows after crash)**: **fix** — startup sweep deletes runs with zero children AND `rendered_path IS NULL` older than 10 min. Target applies to dsp, mix, description runs.
- **OQ-4 (cache growth unbounded)**: **fix** — LRU cap per project on bounce + mix tables (last 200 by `created_at`, evict oldest on insert). Peaks filesystem cache swept by `scenecraft cache gc` CLI.
- **OQ-5 (concurrent writes with same cache key)**: close per INV-1.
- **OQ-6 (peaks orphan files after source edit/delete)**: **fix** — `scenecraft cache gc` purges peaks files whose cache-key pool_segment_id no longer exists or whose mtime+size no longer matches.
- **OQ-7 (cache row present but WAV missing)**: **fix** — stat-check file on cache hit; missing file → treat as miss, re-run. Applies to bounce + mix.

### db-effects-and-curves

- **OQ-1 (effect_type not in frontend registry)**: **codify** — engine preserves unknown effect_type rows; DAL logs warning; frontend renders "unknown effect" placeholder. No engine-side filtering.
- **OQ-2 (send to deleted bus)**: **fix** — FK CASCADE on `track_sends.bus_id`. Send row deleted when bus deleted. Add test.
- **OQ-3 (non-JSON in `static_params`)**: **fix** — DAL validates JSON object on insert/update; non-object (list, scalar, invalid JSON) → `ValueError`. No silent coercion.
- **OQ-4 (`order_index` gaps after delete)**: **codify** — gaps permitted; ordering uses `ORDER BY order_index ASC` which is stable. Add `compact_order_index(table, scope)` DAL helper for explicit renumbering when UI requests it.
- **OQ-5 (delete_effect_curve after effect-delete)**: **codify** — FK cascade on `effect_curves.effect_id` already removes the row; idempotent no-op if called explicitly after.

### db-schema-core-entities

- **OQ-1 (hard-delete keyframe with live transitions)**: **fix** — DAL raises `KeyframeInUseError` if any `transitions.from_kf = kf.id OR to_kf = kf.id` (soft-deleted or not — transition deletion must precede keyframe hard-delete).
- **OQ-2 (audio_clip.track_id → deleted track)**: close — existing cascade (delete_audio_track soft-deletes clips) already handles this.
- **OQ-3 (add_audio_candidate for soft-deleted clip)**: **fix** — DAL raises `AudioClipDeletedError`.
- **OQ-4 (add_tr_candidate for soft-deleted transition)**: **fix** — DAL raises `TransitionDeletedError`.
- **OQ-5 (concurrent reorder audio_tracks)**: close per INV-1.
- **OQ-6 (JSON curve non-monotonic x)**: **fix** — DAL validates monotonic x on insert/update; non-monotonic → `ValueError`. No tolerance-and-sort.
- **OQ-7 (`remap.target_duration < 0`)**: **fix** — `CHECK` constraint on column; reject at DB layer.
- **OQ-8 (legacy `audio_clips.track_id NOT NULL` incomplete migration)**: **fix** — explicit table-rebuild migration (via block 2 target `register_migration` API) re-creates audio_clips with nullable track_id. Transitional: current column-existence check leaves NOT NULL in place on legacy DBs.

### db-undo-redo

- **OQ-1 (API naming mismatch)**: **fix** — rename code to spec'd `begin_undo_group`/`end_undo_group`/`is_undo_capturing`. Keep `undo_begin`/`undo_execute`/`redo_execute` as back-compat aliases through one release cycle.
- **OQ-2 (mutation with current_group=0)**: **codify** — skip capture (treat as "not in a group"). Mutation proceeds but isn't undoable. Add assertion.
- **OQ-3 (redo after new non-undo mutation)**: **codify** — discard redo stack on new mutation outside undo_begin (current behavior). Add test.
- **OQ-4 (undo_log growth within single group)**: **fix** — cap at 10,000 rows per group. On overflow, drop oldest log entry; group becomes partially un-undoable (documented). Add test.
- **OQ-5 (replay across schema migrations)**: **fix** — `undo_log` rows tagged with `schema_version` (from block 2 `schema_migrations` table). Replay fails on schema mismatch with `UndoReplaySchemaVersionMismatch` error; user guided to discard the undo history.
- **OQ-6 (orphan group after process death)**: **fix** — startup sweep closes `undo_groups` with `completed_at IS NULL` older than 1 hour.
- **OQ-7 (multi-writer)**: close per INV-1.
- **OQ-8 (replay failure recovery)**: **fix** — replay wrapped in transaction; on failure, rollback + mark undo_group row `replay_failed=1` + surface error. Group remains in undo_groups but is no longer replayable.

### file-serving-and-uploads

- **OQ-1 (suffix Range `bytes=-500`)**: **fix** — adopt RFC 7233, return last 500 bytes with 206.
- **OQ-2 (multi-range `bytes=0-10,50-60`)**: **fix** — return `multipart/byteranges` per RFC 7233.
- **OQ-3 (invalid Range syntax)**: **fix** — 416 Range Not Satisfiable.
- **OQ-4 (0-byte file + Range)**: **fix** — 416 on any Range request against 0-byte file.
- **OQ-5 (Range start beyond EOF)**: **fix** — 416.
- **OQ-6 (concurrent uploads same `composite_hash`)**: close per INV-1. Upload atomicity via write-to-tmp + rename (same pattern as peaks).
- **OQ-7 (upload body > 2 GiB)**: **fix** — global 200 MB limit on multipart, 1 MB on JSON. 413 Payload Too Large when exceeded. FastAPI request-size middleware.
- **OQ-8 (symlink inside pool pointing outside)**: **fix** — `(project_dir / pool_rel).resolve(strict=True)` + `relative_to(project_dir.resolve())` check. Symlink escape rejected.
- **OQ-9 (audit leak #11 — `startswith` traversal guard)**: **fix** — switch to `Path.relative_to` per OQ-8. Required refactor change, NOT a preserved quirk.
- **OQ-10 (`Last-Modified` frontend reliance)**: **codify** — keep serving `Last-Modified` header (cheap; some HTTP caches rely on it). ETag remains canonical for conditional requests.
- **OQ-11 (`X-Peak-Resolution` echoes requested vs clamped)**: **fix** — echo **clamped** internal value. Frontend treats response header as authoritative.
- **INV-7**: peaks cache remains project-scoped (content-addressed); frame/fragment cache paths migrate to working-copy-scoped.

### generation-pipelines

- **OQ-1 (partial slot success)**: **fix** — keep partials. Generation row gains `status='partial'` + `completed_slots` list. User can retry just the failed slots.
- **OQ-2 (intermediate slot-keyframe disappears mid-gen)**: **fix** — fail the dependent slot with `SlotDependencyError`; partial generation preserved per OQ-1.
- **OQ-3 (`PromptRejectedError` on chat path)**: **fix** — surface as `tool_result` with `isError:true, reason:"prompt_rejected", details:<rejection_reason>`. Chat path matches CLI path's current "log + continue" behavior.
- **OQ-4 (Veo 0-byte downloaded file)**: **fix** — validate downloaded file size > 0; treat 0-byte as `DownloadFailed`. Spend already recorded (Replicate-style idempotent ledger write per INV-3 applies once providers migrate to typed namespace — until then, generation-pipelines captures the contract).
- **OQ-5 (concurrent start for same entity)**: close per INV-1.
- **R10 spend-tracking DEFERRED**: closes as "target state = all providers go through plugin_api.providers with mandatory record_spend (block 4); transitional state documented."

### mcp-bridge

- **OQ-1 (OAuth token expiry mid-session)**: **fix** — detect 401 from MCP server; re-fetch token via OAuth refresh flow; retry the call once. On refresh failure, degraded-mode (tools hidden).
- **OQ-2 (malformed tool schema)**: **fix** — skip that tool, log warning with schema snippet. Bridge continues with valid tools.
- **OQ-3 (cross-service tool name collision)**: **fix** — namespace everything as `{service}__{tool}`. Remember tools stay un-prefixed for back-compat through one release cycle; future services MUST use the prefix.
- **OQ-4 (legitimate long Remember queries)**: **fix** — raise `call_tool` timeout from 60s to 300s. Match elicitation timeout.
- **OQ-5 (initial connect failure + long-lived session)**: **fix** — retry with exponential backoff up to 5 min total. On eventual success, emit `core__chat__mcp_tools_ready` (matches chat-pipeline OQ-5).
- **OQ-6 (concurrent connect same service)**: **fix** — idempotent — `asyncio.Lock` per service prevents concurrent connects; second caller awaits the first's result.

### migrations-framework

- **OQ-1 (no `schema_migrations` version table)**: **fix** — target state includes per-project `schema_migrations(version, applied_at, applied_by)` table. Current column-existence check transitional.
- **OQ-2 (`register_migration` plugin primitive — M17 design still target?)**: **codify** — yes. Target: `plugin_api.register_migration(version, up_fn, down_fn=None)`. Called during plugin activation. Migrations applied in version-order across all plugins + core on project open.
- **OQ-3 (rollback semantics)**: **codify** — `down_fn` optional. If provided, `scenecraft migrate down --to <version>` walks migrations in reverse. If not provided, rollback is not supported and migration docs MUST note this.
- **OQ-4 (legacy DB with pre-existing `NOT NULL`)**: **fix** — target includes table-rebuild helper (`migrate.rebuild_table(name, new_schema, row_transform=None)`) that CREATEs a temp table, copies rows through an optional transform, swaps atomically. Current DB.py approach (ALTER TABLE ADD COLUMN additive-only) is transitional.
- **OQ-5 (plugin CHECK constraints)**: **codify** — supported via `rebuild_table` helper. CHECK constraints can only be introduced via full table rebuild, not ALTER.
- **OQ-6 (data migrations beyond single-pass UPDATE)**: **codify** — migrations may run arbitrary Python. `up_fn(conn)` gets the SQLite connection; any SQL including multi-statement is valid.
- **OQ-7 (concurrent schema init across OS processes)**: **fix** — advisory `flock` on `.scenecraft/schema.lock` during `_ensure_schema` + migration apply.
- **INV-8 transitional section**: spec gains "Transitional Behavior" subsection documenting current additive-ALTER / column-existence-check approach until `register_migration` lands.

### plugin-loading-lifecycle

- **OQ-1 (dependency ordering)**: **fix** — target includes `requires: [plugin_id]` field on plugin manifest. Topological-sort on activation. Cycle detection raises `PluginCycleError` at boot.
- **OQ-2 (activate exceptions fatal)**: **fix** — target = atomic activation: wrap `plugin.activate()` in try/except; on raise, LIFO-dispose already-registered contributions for that plugin + log clear error + continue to next plugin. Engine boot succeeds with partial plugin set. Current fatal-on-exception behavior transitional.
- **OQ-3 (shutdown hook worth adding)**: **fix** — `PluginHost.deactivate_all()` called on SIGINT/SIGTERM (matches server-bootstrap OQ-4). Each plugin's Disposables fire LIFO. Daemon threads joined with 5s timeout.
- **OQ-4 (register_migration API)**: close per migrations-framework OQ-2 (now in target spec).
- **OQ-5 (generate_foley not registered anywhere)**: **fix** — immediate: add generate_foley to both hardcoded-import lists in api_server.py and mcp_server.py. Target: filesystem scan replaces hardcoded lists (see OQ-7).
- **OQ-6 (hot reload in dev)**: **defer** — target eventually, but not blocking. `plugin_host.reload(name)` API reserved for future dev-mode milestone. `**Deferred**: dev-mode feature; not blocking the FastAPI refactor`.
- **OQ-7 (two activation paths — api_server.py + mcp_server.py)**: **fix** — consolidate to single filesystem-scan discovery: walk `src/scenecraft/plugins/*/` directories, look for `plugin.yaml`, register via unified path. Core allowlist (hardcoded list of plugin_ids-to-load) overrides scan. Current dual-hardcoded-list transitional.

### providers-typed-and-legacy

- **OQ-1 (Veo/Imagen infinite retry)**: **fix** — cap at 5 attempts × exponential backoff with jitter, max 60s between attempts. `ReplicateError`-style exception hierarchy ported.
- **OQ-2 (Kling spend attribution)**: **fix** — migrate Kling to `plugin_api.providers.kling` typed namespace with mandatory `record_spend()` before download (INV-3 idempotent).
- **OQ-3 (Anthropic token rotation mid-stream)**: **codify** — token read per-call at `_auth_headers()`; rotation takes effect on next HTTP attempt. Documented contract (matches Replicate pattern).
- **OQ-4 (google-genai SDK version pin)**: **fix** — pin in pyproject.toml + import-time compat check.
- **OQ-5 (Musicful poll worker never terminal)**: **fix** — 30-minute wall-clock timeout; on expiry, generation row marked `status='failed', error='polling timeout'`. Spend NOT recorded (no completion). User can retry.
- **OQ-6 (simultaneous calls to same provider)**: close per INV-1.
- **OQ-7 (ai/provider.py vs chat.py Anthropic duplication — 6 call sites)**: **fix** — consolidate all Anthropic calls behind `plugin_api.providers.anthropic` typed namespace. 6 call sites converge to one provider module. Transitional: current scatter documented.
- **INV-8 transitional section**: spec captures current direct-SDK / legacy-call_service scatter alongside the target unified `plugin_api.providers.<name>` surface for all 9 providers. Migration sequence listed: Replicate (done), Musicful → replace legacy shim, Imagen/Veo → new typed provider, Kling → new typed provider, Runway → new typed provider, Anthropic → new typed provider, Google GenAI → new typed provider.

### render-pipeline

- **OQ-1 (schedule rebuild mid-render)**: close per cache-invalidation OQ-4 (render holds schedule from t=0 — snapshot semantics).
- **OQ-2 (cv2.VideoWriter failure unchecked)**: **fix** — check `out.write(frame)` return; on False raise `RenderError("VideoWriter rejected frame at t=<time>")`. No proceeding to mux.
- **OQ-3 (ffmpeg missing from PATH)**: **fix** — preflight check in `assemble_final` via `shutil.which("ffmpeg")`. Missing → `MissingDependencyError("ffmpeg not found; install via: <platform-specific hint>")` before any work.
- **OQ-4 (orphan `.tmp.mp4` on crash)**: **fix** — `try/finally` wrapping mux; `.tmp.mp4` always cleaned up on exit path.
- **OQ-5 (transform curve x > 1.0)**: **codify** — clamp to [0,1] in `_evaluate_curve`; contract R_N.
- **OQ-6 (scale=0 short-circuits to identity)**: **fix** — scale=0 returns a black frame of target dimensions (NumPy zeros). Current identity-return behavior is a bug; fix in refactor.
- **OQ-7 (INTER_LINEAR vs INTER_AREA inconsistency)**: **fix** — codify: INTER_AREA for downscale (new_w*new_h < w*h), INTER_LINEAR for upscale or identity. Apply uniformly across all cv2.resize call sites.
- **OQ-8 (zero-duration schedule)**: **fix** — short-circuit before opening VideoWriter; return success with no output file (or optionally an empty 0-frame file, per product preference — TBD). Add test.

### rest-api-dispatcher

- **OQ-1 (`@require_paid_plugin_auth` dead)**: **fix** — preserve decorator; spec captures policy "endpoints flagged `paid: true` in plugin.yaml MUST require the double-gate." Decorator applied at FastAPI port when paid plugins ship. No endpoints today.
- **OQ-2 (CORS no allowlist)**: **fix** — allowlist from `config.json:cors_origins` array. Default `["http://localhost:5173"]` + configured tunnel domain. Reject all others.
- **OQ-3 (path traversal guard)**: close per file-serving OQ-9 (switch to `Path.relative_to`).
- **OQ-4 (plugin REST GET+POST only)**: **fix** — forward GET, POST, DELETE, PATCH, PUT. Current limit is a shipped bug.
- **OQ-5 (no body-size cap)**: close per file-serving OQ-7 (200 MB multipart, 1 MB JSON).
- **OQ-6 (concurrent uploads same dest)**: close per INV-1.
- **OQ-7 (duplicate `scenecraft_jwt` cookies)**: **fix** — accept first, reject rest with 400 `MALFORMED_REQUEST`.
- **OQ-8 (Range end < start)**: close per file-serving OQ-3 (416).
- **OQ-9 (multi-range)**: close per file-serving OQ-2 (multipart/byteranges).
- **OQ-10 (very long URLs > 8KB)**: **fix** — 8 KB hard cap; 414 URI Too Long.
- **OQ-11 (ACL missing paid-plugin headers)**: **fix** — add `X-Scenecraft-API-Key` + `X-Scenecraft-Org` to `Access-Control-Allow-Headers`.
- **OQ-12 (cookie refresh — last_active_org claim)**: **fix** — new token carries forward `last_active_org` claim from refreshed cookie's payload. Not re-derived from DB.
- **OQ-13 (`Expect: 100-continue`)**: **codify** — uvicorn handles natively; spec says accept.
- **OQ-14 (`Transfer-Encoding: chunked`)**: **fix** — accept in FastAPI port. Current reject-on-stdlib (returns 400 Empty body) transitional.
- **OQ-15 (DELETE on missing view)**: **fix** — idempotent 200 with `{deleted: false}`. Matches M13 DELETE pattern.
- **OQ-16 (uncaught handler exceptions)**: **fix** — FastAPI exception handler emits `{error: {code: "INTERNAL_ERROR", message: "..."}}`. Migration Contract item 22 binding.
- **OQ-17 (OPTIONS Access-Control-Max-Age)**: **fix** — add `Access-Control-Max-Age: 3600`.
- **OQ-18 (JWT missing `sub`)**: **fix** — treat as 401 `MALFORMED_TOKEN` at dispatcher level; `_authenticated_user` never None when authenticated.

### server-bootstrap

- **OQ-1 (WS port bind failure swallowed)**: **fix** — WS daemon thread signals boot thread via `threading.Event`; main thread checks within 5s and aborts boot with clear error if WS bind failed.
- **OQ-2 (work_dir unreadable)**: **fix** — preflight `os.access(work_dir, R_OK|W_OK)` + clear error.
- **OQ-3 (config.json corrupted)**: **fix** — wrap `json.load` in try/except; on `JSONDecodeError`, abort with "invalid JSON in config.json; remove file to re-initialize."
- **OQ-4 (SIGTERM no handler)**: **fix** — install `signal.signal(SIGTERM, ...)` handler mirroring SIGINT. Calls `PluginHost.deactivate_all()` then `server.shutdown()`.
- **OQ-5 (`--no-auth` in production)**: **fix** — if `.scenecraft/` root exists + `--no-auth` passed, require `--no-auth-unsafe-i-know-what-im-doing` flag. Otherwise refuse with clear message.
- **OQ-6 (multiple concurrent server instances)**: **fix** — advisory `flock` on `.scenecraft/server.lock` at boot. Refuse to start if held, clear error.

---

## Commit

After all spec-patching agents complete, do one commit in the engine repo:
```
docs(specs): resolve ~144 OQs across engine specs (target-ideal spec pattern)

Batched engine-side OQ resolution per agent/reports/oq-resolution-decisions-2026-04-27.md.
~144 OQs closed; default approach: spec target-ideal state, capture current code as
transitional behavior where divergent. New invariants: INV-7 (per-working-copy frame
cache), INV-8 (target/transitional distinction convention). Many OQs close to
cross-cutting INVs (INV-1 single-writer, INV-7 cache isolation). Plugin lifecycle,
migrations framework, and provider surface all spec'd to their target-ideal contracts
with current code marked transitional pending FastAPI refactor milestone.
```

Exclude other working-tree changes.
