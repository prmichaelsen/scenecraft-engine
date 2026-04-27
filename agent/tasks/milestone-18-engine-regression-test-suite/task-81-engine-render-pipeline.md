# Task 81: Engine Render Pipeline Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-render-pipeline`](../../specs/local.engine-render-pipeline.md)
**Design Reference**: [`local.engine-render-pipeline`](../../specs/local.engine-render-pipeline.md)
**Estimated Time**: 6-10 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-render-pipeline.md`. Lock in: `build_schedule` output shape, `render_frame_at` determinism, proxy preference, fragment cache interactions, background renderer coordination, and the byte-identical JPEG contract for `/render-frame`. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    def test_render_frame_byte_identical(self, engine_server):
        """covers Rn (e2e)"""
        # GET /render-frame?t=10.0 twice; assert byte-identical JPEG bytes.
        # Assert also matches a committed golden fixture.
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
- [ ] E2E section present with byte-identical golden fixture assertion
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
