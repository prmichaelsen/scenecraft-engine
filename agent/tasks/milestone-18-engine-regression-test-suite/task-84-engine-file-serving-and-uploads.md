# Task 84: Engine File Serving + Uploads Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-file-serving-and-uploads`](../../specs/local.engine-file-serving-and-uploads.md)
**Design Reference**: [`local.engine-file-serving-and-uploads`](../../specs/local.engine-file-serving-and-uploads.md)
**Estimated Time**: 6-8 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-file-serving-and-uploads.md`. Lock in: Range-aware GET, HEAD, multipart upload, path-traversal rejection, MIME detection, and the exact headers (`Content-Range`, `Accept-Ranges`, `Content-Length`). Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

File serving and uploads touch Range requests, HEAD, multipart, and path-traversal — all fragile. The M16 spec calls out Range parity as the trickiest migration piece. This spec is the contract M16 must not regress. Builds on task-70 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-file-serving-and-uploads.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_file_serving_and_uploads.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- GET without Range — 200, `Accept-Ranges: bytes`, full body.
- GET with Range — 206, `Content-Range: bytes <a>-<b>/<total>`, body exactly `[a..b]`.
- GET with invalid Range — 416, `Content-Range: bytes */<total>`.
- Suffix range — reject per spec (spec says 416).
- HEAD — 200, `Content-Length`, `Accept-Ranges: bytes`, empty body.
- Path traversal — `../` in path → 404 (not 500).
- MIME detection — `.mp4`, `.jpg`, `.wav` — correct `Content-Type`.
- Multipart upload — POST with `multipart/form-data` → file lands at expected path.
- Large upload — streaming, not buffered.

Target-ideal behaviors (e.g., `If-Range`, conditional `ETag`) → `xfail`.

### 4. Cover every Behavior Table row

### 5. Add e2e section (primary for this spec)

```python
# === E2E ===

class TestEndToEnd:
    """E2E is primary for this spec — file serving IS an HTTP surface."""

    def test_get_range_206(self, engine_server, project_dir): ...
    def test_get_range_416_invalid(self, engine_server, project_dir): ...
    def test_head_metadata_only(self, engine_server, project_dir): ...
    def test_path_traversal_rejected(self, engine_server, project_dir): ...
    def test_multipart_upload(self, engine_server, project_dir): ...
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_file_serving_and_uploads.py -v
git add tests/specs/test_engine_file_serving_and_uploads.py
git commit -m "test(M18-84): engine-file-serving-and-uploads regression tests — <N> unit + <M> e2e"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present (primary for this spec)
- [ ] `pytest ... -v` passes

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Every header asserted explicitly | Yes | M16 spec calls out header parity; exact match required. |
| Streaming tested with large file | Yes | Non-streaming reads OOM on big files; catch regressions. |

---

## Notes

- These tests mirror M16 tasks T57 + T63 — keep them independent; don't depend on FastAPI code.
- Use `httpx.Client` against the current server for portability.
