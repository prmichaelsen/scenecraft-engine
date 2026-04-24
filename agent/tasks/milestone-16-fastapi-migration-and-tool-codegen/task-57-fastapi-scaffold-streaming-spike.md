# Task 57: FastAPI scaffold + streaming spike

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.fastapi-migration`](../../specs/local.fastapi-migration.md) — R1–R3, R12, R20–R23, R29–R31
**Estimated Time**: 4–6 hours
**Dependencies**: None
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Stand up the FastAPI app skeleton and prove the two trickiest mechanics end-to-end before touching any of the 164 business routes: **Range-aware file streaming** (the most fragile part of the migration) and **`/openapi.json` emission with `operationId`s** (the load-bearing output for Phase B). Exit criterion: one file-streaming test and one openapi-shape test pass under `TestClient(app)`, and `uvicorn.run(app)` serves a single `GET /api/config` route for smoke.

---

## TDD Plan

Write the tests listed under **Tests to Pass** below **before** implementing. They all must fail at first (`test_file_get_range_206`: no such route; `file_traversal_rejected`: no such route; etc.). Then implement `app.py`, `deps.py`, `errors.py`, `streaming.py`, and the tiny initial router until every listed test passes. No other routes are ported in this task.

---

## Steps

### 1. Add dependencies

Edit `pyproject.toml`:
```toml
dependencies = [
    ...existing...,
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "python-multipart>=0.0.9",
]

[project.optional-dependencies]
dev = [
    ...existing...,
    "openapi-spec-validator>=0.7",
    "httpx>=0.27",
]
```

### 2. Create the API package

```
src/scenecraft/api/
    __init__.py
    app.py
    deps.py
    errors.py
    streaming.py
    models/__init__.py
    routers/__init__.py
    routers/misc.py          # hosts the GET /api/config spike
    routers/files.py         # hosts GET/HEAD /api/projects/{name}/files/{file_path:path}
```

### 3. `app.py` — minimal app factory

```python
from fastapi import FastAPI
from scenecraft.api.errors import install_exception_handlers
from scenecraft.api.deps import install_cors
from scenecraft.api.routers import misc, files

def create_app(work_dir=None, *, enable_docs: bool = True) -> FastAPI:
    app = FastAPI(
        openapi_url="/openapi.json",
        docs_url="/docs" if enable_docs else None,
        redoc_url="/redoc" if enable_docs else None,
        title="scenecraft-engine",
    )
    install_cors(app)
    install_exception_handlers(app)
    app.include_router(misc.router)
    app.include_router(files.router)
    app.state.work_dir = work_dir
    return app

app = create_app()
```

### 4. `errors.py` — legacy envelope handlers

- `class ApiError(HTTPException)` — adds `code: str`.
- `async def _http_exception_handler(request, exc)` — emit `{"error": <CODE>, "message": <detail>}`; map FastAPI 404 to `NOT_FOUND`, 401 to `UNAUTHORIZED`, etc.
- `async def _validation_handler(request, exc)` — flatten first Pydantic error into `{"error": "BAD_REQUEST", "message": "..."}` with 400 status (NOT default 422).
- `async def _unhandled_exception_handler(request, exc)` — log traceback, return 500 `{"error": "INTERNAL_ERROR", "message": "..."}`.

### 5. `deps.py` — CORS install + placeholder auth

- `install_cors(app)` — install `CORSMiddleware` with exact legacy allow-origin list (audit `api_server.py::_cors_headers` for the values).
- `async def current_user(...) -> User` — placeholder that accepts any request for this task (real implementation lands in T58). Leave a `TODO: T58` comment.

### 6. `streaming.py` — Range-aware file response

Implement `file_response_with_range(path: Path, request: Request) -> Response`:
- Reject path traversal (resolved path must be under `request.app.state.work_dir`); return 404 if not.
- Parse `Range: bytes=<start>-<end>`. Reject suffix ranges (`bytes=-N`) with 416.
- If start >= file_size, return 416 with `Content-Range: bytes */<size>`.
- If no Range, return `Response(content=file_bytes, media_type=mime, headers={"Accept-Ranges": "bytes", "Content-Length": str(size)})`.
- If Range present, return `StreamingResponse(range_iterator(path, start, end), status_code=206, media_type=mime, headers={"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)})`.
- Stream chunks of `min(65536, remaining)` bytes (matches legacy).

### 7. `routers/files.py` — GET + HEAD for files

```python
router = APIRouter(prefix="/api/projects", tags=["files"])

@router.get(
    "/{name}/files/{file_path:path}",
    operation_id="get_project_file",
    summary="Serve a file from the project directory",
    response_class=Response,  # raw bytes or stream
)
async def get_file(name: str, file_path: str, request: Request):
    full = (request.app.state.work_dir / name / file_path).resolve()
    return file_response_with_range(full, request)

@router.head(
    "/{name}/files/{file_path:path}",
    operation_id="head_project_file",
    summary="Metadata (Content-Length, Accept-Ranges) for a project file",
)
async def head_file(...): ...  # 200 + headers, empty body
```

### 8. `routers/misc.py` — `GET /api/config` spike

A minimal route that returns `load_config()` as JSON. Auth deferred to T58.

### 9. Tests to Pass

Create `tests/test_fastapi_scaffold.py` with the cases below. Use `fastapi.testclient.TestClient` with a temp `work_dir` fixture.

- `file_get_no_range` — write a 100 KB fixture file; GET without Range returns 200, body length 102400, body bytes match file, `Accept-Ranges: bytes` header present.
- `file_get_range_206` — same fixture; `Range: bytes=0-999`; expect 206, `Content-Range: bytes 0-999/102400`, body exactly 1000 bytes, body equals `file_bytes[0:1000]`.
- `file_get_range_416` — `Range: bytes=200000-300000`; expect 416 with `Content-Range: bytes */102400`.
- `file_get_suffix_range_416` — `Range: bytes=-100`; expect 416.
- `file_head_metadata_only` — HEAD on the fixture; expect 200, `Content-Length: 102400`, `Accept-Ranges: bytes`, empty body.
- `file_traversal_rejected` — GET `/api/projects/P1/files/../other/secret.txt`; expect 404 with envelope `{"error": "NOT_FOUND", ...}`.
- `openapi_valid_3_1` — GET `/openapi.json`; `openapi_spec_validator.validate(spec)` passes; `spec["openapi"] >= "3.1.0"`.
- `swagger_ui_renders` — GET `/docs`; 200; body contains `swagger-ui`.

### 10. Audit call sites (OQ-3)

`git grep "make_handler\|api_server" src/ tests/ scripts/` — list every call site that constructs the legacy server. The plan is to migrate these in T60–T64 (production code) and T65 (test fixtures). Record the list in this task's PR description so nothing is missed.

---

## Verification

- [ ] `pyproject.toml` lists `fastapi`, `uvicorn[standard]`, `python-multipart`, `openapi-spec-validator`, `httpx`
- [ ] `src/scenecraft/api/` package imports cleanly
- [ ] All 8 named tests in `tests/test_fastapi_scaffold.py` pass
- [ ] `GET /openapi.json` returns a valid OpenAPI 3.1 doc
- [ ] `uvicorn scenecraft.api.app:app --port 8890` serves `/api/config` and `/files/*`
- [ ] Legacy `api_server.py` is **untouched** in this task (it stays running for the rest of Phase A)
- [ ] PR description includes the `api_server` call-site audit (OQ-3 resolution)

---

## Tests Covered (from spec)

From `local.fastapi-migration`:
- `file-get-no-range` (R20, R22)
- `file-get-range-206` (R21)
- `file-get-range-416` (R21)
- `file-get-suffix-range-416` (R21)
- `file-head-metadata-only` (R12)
- `file-traversal-rejected` (R22)
- `openapi-valid-3-1` (R29)
- `swagger-ui-renders` (R31)

---

## Notes

- `/render-frame` byte-parity and perf baseline are NOT part of this spike — they land in T63 and T65.
- Auth is stubbed here and implemented in T58. All routes added after T58 will have the real `current_user` dependency.
- The legacy server keeps running; both `api_server.py` and the new app can serve side by side until T65's hard cut.
