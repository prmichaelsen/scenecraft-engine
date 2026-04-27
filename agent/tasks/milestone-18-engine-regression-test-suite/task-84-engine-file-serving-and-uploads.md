# Task 84: Engine File Serving + Uploads Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-file-serving-and-uploads`](../../specs/local.engine-file-serving-and-uploads.md)
**Design Reference**: [`local.engine-file-serving-and-uploads`](../../specs/local.engine-file-serving-and-uploads.md)
**Estimated Time**: 12 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Unit tests may mock; e2e MUST NOT. Lock in: Range-aware GET, HEAD, multipart upload, path-traversal rejection, MIME detection, and the exact headers (`Content-Range`, `Accept-Ranges`, `Content-Length`). Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. E2E coverage checklist (comprehensive)

File serving IS an HTTP surface. E2E is primary and MUST cover every row in the spec's Behavior Table.

Endpoints / scenarios:

- `GET /files/<path>` without Range → 200, `Accept-Ranges: bytes`, full body, correct `Content-Length`
- `GET` with `Range: bytes=0-99` → 206, `Content-Range: bytes 0-99/<total>`, body exactly those bytes
- `GET` with `Range: bytes=100-` (open-ended) → 206, correct slice
- `GET` with invalid Range (start > size) → 416, `Content-Range: bytes */<total>`
- `GET` with suffix Range `bytes=-10` → per-spec (likely 416)
- `GET` with multiple Range specs `bytes=0-10,20-30` → per-spec (likely 416 or multipart)
- `HEAD /files/<path>` → 200, `Content-Length`, `Accept-Ranges: bytes`, empty body
- Path traversal `GET /files/../../etc/passwd` → 404 (not 500, not 200)
- URL-encoded traversal `GET /files/%2e%2e%2fetc%2fpasswd` → 404
- MIME: `.mp4` → `video/mp4`; `.jpg` → `image/jpeg`; `.wav` → `audio/wav`; `.json` → `application/json`; unknown ext → octet-stream
- `POST /files/upload` multipart/form-data → file lands at expected project-scoped path; verifiable via subsequent GET
- Upload with path-traversal name → rejected
- Large file upload (10MB+) — streaming verified (memory stays bounded; response succeeds)
- Large file GET — streaming verified
- Concurrent GETs on the same file — both succeed
- Auth enforced on upload (401 without cookie)
- Auth NOT required on public files per spec (or enforced — follow spec)
- `POST /bounce-upload` and `POST /mix-render-upload` — project-scoped content-addressable paths
- Target-state xfails: `If-Range`, conditional `ETag`, gzip content-encoding

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — every Range + HEAD + upload + traversal + MIME case."""
    # ... tests per checklist
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
- [ ] E2E section present with comprehensive Range + HEAD + upload + traversal + MIME coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
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
