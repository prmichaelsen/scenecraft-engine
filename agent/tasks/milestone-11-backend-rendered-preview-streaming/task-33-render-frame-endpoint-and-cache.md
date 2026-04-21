# Task 33: `GET /render-frame` endpoint + L1 memory cache

**Milestone**: [M11 - Backend-Rendered Preview Streaming](../../milestones/milestone-11-backend-rendered-preview-streaming.md)
**Design Reference**: [backend-rendered-preview-streaming](../../design/local.backend-rendered-preview-streaming.md)
**Estimated Time**: 2-3 days
**Dependencies**: Task 32
**Status**: Completed
**Completed Date**: 2026-04-20
**Actual Commits**: `85af264`, `cbe7a65`

---

## Objective

Expose the backend compositor as an HTTP endpoint for scrub/paused frame previews, backed by an LRU cache that invalidates automatically on any project DB write.

---

## Context

Scrubbing hits the backend on every pointer-move (or throttled equivalent). Without caching, every request re-renders from cold — compositor throughput at 5-15 fps on CPU would make scrubbing unusable. An in-memory LRU keyed on the project's SQLite mtime gives free cache invalidation: any DB write bumps mtime → new key → miss → re-render.

---

## Steps

### 1. Single-frame HTTP endpoint
- `GET /api/projects/:name/render-frame?t=<seconds>[&quality=<1-100>]`
- Returns `image/jpeg` bytes encoded via OpenCV
- Default quality 85; clamped to [1, 100]
- `t` clamped to `[0, duration - 1/fps]`
- 404 on unknown project or empty timeline (no segments)
- 400 on malformed `t`
- `Cache-Control: no-store`
- `X-Scenecraft-Cache: HIT|MISS` debug header

### 2. Frame cache module (`src/scenecraft/render/frame_cache.py`)
- `FrameCache` class: thread-safe LRU
- Defaults: 500 frames OR 250 MB (whichever first)
- Key: `(project_dir_str, max(db mtime, wal mtime), t_ms, quality)`
- `get`, `put`, `invalidate_project`, `stats`, `clear`
- Module-global `global_cache` instance

### 3. SQLite WAL awareness
- Reads mtime from both `project.db` and `project.db-wal`, uses max
- Every write touches `.db-wal` immediately even if main `.db` mtime hasn't ticked on checkpoint

### 4. Stats endpoint
- `GET /api/render-cache/stats` returns `{frames, bytes, max_frames, max_bytes, hits, misses, hit_rate}`

### 5. Tests (`tests/test_render_frame_endpoint.py`)
- Returns valid JPEG bytes (magic bytes + length)
- Quality param changes byte length proportionally
- Out-of-range t clamps instead of erroring
- Malformed t returns 400
- Unknown project returns 404
- Empty project returns 404
- Second request hits cache (HIT header)
- DB write invalidates cache (next request is MISS)

---

## Verification

- [x] Endpoint returns JPEG with correct magic bytes
- [x] Cache HIT/MISS header accurate on repeated requests
- [x] Cache invalidates on `set_meta` (via WAL mtime bump)
- [x] Stats endpoint returns correct shape
- [x] All 8 endpoint tests + 3 parity tests pass together (11 total)

---

## Expected Output

### Files Created
- `src/scenecraft/render/frame_cache.py`
- `tests/test_render_frame_endpoint.py`

### Files Modified
- `src/scenecraft/api_server.py` — route + handler + stats endpoint

---

## Notes

- Fine-grained range-based invalidation (per design §2.3) is deferred. Any DB write today flushes the whole project's cache.
- Per-session / per-user cache scoping (design §4.3) also deferred — MVP uses a single process-global LRU.

---

**Status**: Completed
