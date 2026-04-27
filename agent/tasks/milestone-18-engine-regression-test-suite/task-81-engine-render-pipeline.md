# Task 81: Engine Render Pipeline Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-render-pipeline`](../../specs/local.engine-render-pipeline.md)
**Design Reference**: [`local.engine-render-pipeline`](../../specs/local.engine-render-pipeline.md)
**Estimated Time**: 14 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Unit tests may mock; e2e MUST NOT. Lock in: `build_schedule` output shape, `render_frame_at` determinism, proxy preference, fragment cache interactions, background renderer coordination, and the byte-identical JPEG contract for `/render-frame`. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

The render pipeline is the performance-critical hot path — proxies, fragment cache, background renderer, compositor. This spec locks the build_schedule + render_frame_at contract and the byte-identical JPEG invariant for /render-frame. Builds on task-70 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-render-pipeline.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_render_pipeline.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- `build_schedule` output — given a timeline fixture, assert the schedule is deterministic and well-formed.
- `render_frame_at(t)` determinism — same input, same output (byte-identical).
- Proxy preference — proxy exists at requested resolution → read from proxy, not source.
- Compositor fallback — proxy missing → falls back to source with correct offset.
- Fragment cache hit — render a range twice; second call skips compositor.
- Background renderer priority — prime_around_playhead orders by distance.
- Encoder generation bump — settings change → encoder rebuilds.
- Chunked proxies — source ≥ chunk_seconds uses chunked mode; reads pick the right chunk.

Target-ideal behaviors → `xfail`.

### 4. Cover every Behavior Table row

### 5. E2E coverage checklist (comprehensive)

The render pipeline has the largest HTTP+WS surface. E2E MUST exercise every documented endpoint, cache-stat endpoint, render-state endpoint, and WS event.

Endpoints / WS events:

- `GET /api/projects/:name/render-frame?t=<s>` — byte-identical twice; matches golden JPEG fixture
- GET with various `t` values including 0, midpoint, near-end
- GET with invalid `t` (negative, beyond timeline) → 400 envelope
- GET on a proxy-enabled project — served from proxy (observable via `/render-cache/stats` OR timing)
- GET on a non-proxy project — falls back to source with correct offset (frame content matches)
- `GET /render-cache/stats` — contains both `frames` and `fragments` counts
- `GET /render-state` — snapshot shape; buckets reflect rendered ranges
- WS: `/preview-stream` subscribes, play() begins, frames stream; assert event order
- Background renderer: `POST /api/projects/:name/prime-around-playhead?t=X` (if exposed) → subsequent GET at nearby t is a cache hit
- Settings mutation → encoder generation bump → next frame re-encoded (observable via content change AND stats)
- Chunked proxies: a source ≥ chunk_seconds triggers chunked mode; reads pick correct chunk (observable via no-error + content)
- Proxy mode switch (`prefer_proxy=false`) via settings endpoint → subsequent renders from source
- `build_schedule` determinism via GET `/api/projects/:name/schedule` (if exposed)
- Target-state xfails: HEAD on /render-frame returning Content-Length without body, streaming content-type auto-negotiation

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — every render endpoint + cache-stats + WS preview events."""
    # ... tests per checklist
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_render_pipeline.py -v
git add tests/specs/test_engine_render_pipeline.py tests/specs/fixtures/render-frame.golden.jpg
git commit -m "test(M18-81): engine-render-pipeline regression tests — <N> unit + <M> e2e"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present with byte-identical golden + comprehensive endpoint + WS coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
- [ ] `pytest ... -v` passes

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Golden JPEG fixture | Yes | M16 spec explicitly calls out byte-identical parity as the migration gate. |
| Proxy + compositor both tested | Yes | Fallback path is where bugs hide. |
| Background renderer priority test | Yes | Distance-from-playhead ordering is subtle. |

---

## Notes

- Golden fixture should be small (a few KB) — test a plain-color timeline, not a real video.
- If the current engine's JPEG output isn't deterministic across runs (e.g., timestamp metadata), file as a bug and xfail the byte-identical test with a specific reason.
