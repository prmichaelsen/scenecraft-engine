# Task 63: Rendering + files + pool + candidates routers

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.fastapi-migration`](../../specs/local.fastapi-migration.md) — R4, R6–R8, R20–R24, R51, R52
**Estimated Time**: 8 hours
**Dependencies**: T57, T58, T59
**Status**: Not Started

---

## Objective

Port the media / streaming / generation cluster: render-frame (the hot path), render-state + render-cache stats, thumbnails/filmstrip/download-preview, pool CRUD and upload (multipart + large-file streaming), candidate generation + listing, and the staging/promotion routes. Roughly 35 routes. This task is where streaming correctness gets its full workout — `/render-frame` byte-parity and `/pool/upload` large-file streaming both live here.

---

## TDD Plan

Capture parity fixtures, including **a byte-identical JPEG fixture for `/render-frame`** against the legacy server at 5 representative `(project, t, quality)` tuples. Write the streaming tests including byte-diff assertions. Port routes incrementally.

---

## Steps

### 1. `/render-frame` byte-parity fixtures

Extend `scripts/capture_parity_fixtures.py` with a `--render-frame` mode:
- For each of 5 `(project, t, quality)` tuples across 2 test projects, call the legacy `/render-frame` endpoint.
- Save the raw JPEG bytes to `tests/fixtures/parity/render_frame_<project>_<t>_<quality>.jpg`.
- These fixtures are **binary** — commit them directly.

### 2. Pydantic models (`src/scenecraft/api/models/media.py`)

- `RenderFrameQuery` (t: float, quality: int = 80)
- `PoolAddBody`, `PoolImportBody`, `PoolRenameBody`, `PoolTagBody`, `PoolUntagBody`, `PoolGcBody`
- `AssignPoolVideoBody`
- `CandidatePromoteBody`, `StagedCandidateBody`
- `DownloadPreviewQuery` (start, end)
- `FilmstripQuery` (t, height)
- `ThumbQuery`

### 3. Routers

#### `routers/rendering.py`

- `GET /api/projects/{name}/render-frame` → `get_render_frame` (the JPEG hot path)
  - Use `Response(content=jpeg_bytes, media_type="image/jpeg", headers={"Cache-Control": "..."})`
  - MUST produce byte-identical output to legacy (no double-encoding, no recompression).
- `GET /api/projects/{name}/render-state` → `get_render_state`
- `GET /api/render-cache/stats` → `get_render_cache_stats`
- `GET /api/projects/{name}/download-preview` → `download_preview` (streaming)
- `GET /api/projects/{name}/thumb/{path:path}` → `get_thumb`
- `GET /api/projects/{name}/thumbnail/{path:path}` → `get_thumbnail`
- `GET /api/projects/{name}/transitions/{tr_id}/filmstrip` → `get_filmstrip`

#### `routers/files.py` (extend from T57)

- GET + HEAD for `/files/{path:path}` already done in T57. Nothing new here unless an audit reveals missing routes.
- `GET /api/projects/{name}/descriptions` → `get_descriptions`

#### `routers/pool.py`

- `GET /api/projects/{name}/pool` → `get_pool`
- `GET /api/projects/{name}/pool/tags` → `get_pool_tags`
- `GET /api/projects/{name}/pool/gc-preview` → `pool_gc_preview`
- `GET /api/projects/{name}/pool/{seg_id}/peaks` → `get_pool_segment_peaks`
- `POST /api/projects/{name}/pool/add` → `pool_add`
- `POST /api/projects/{name}/pool/import` → `pool_import`
- `POST /api/projects/{name}/pool/upload` → `pool_upload` (multipart, must stream to disk — not buffer in memory)
- `POST /api/projects/{name}/pool/rename` → `pool_rename`
- `POST /api/projects/{name}/pool/tag` → `pool_tag`
- `POST /api/projects/{name}/pool/untag` → `pool_untag`
- `POST /api/projects/{name}/pool/gc` → `pool_gc`
- `POST /api/projects/{name}/assign-pool-video` → `assign_pool_video`

#### `routers/candidates.py`

- `GET /api/projects/{name}/unselected-candidates` → `list_unselected_candidates`
- `GET /api/projects/{name}/video-candidates` → `list_video_candidates`
- `GET /api/projects/{name}/staging/{stagingId}` → `get_staging`
- `POST /api/projects/{name}/promote-staged-candidate` → `promote_staged_candidate`
- `POST /api/projects/{name}/generate-staged-candidate` → `generate_staged_candidate`

#### `routers/effects.py` (transitions/keyframe effects — not M13 audio effects)

- `GET /api/projects/{name}/effects` → `list_effects`
- `POST /api/projects/{name}/effects` → `upsert_effects`

### 4. Large-upload streaming

The `pool_upload` handler MUST stream multipart data to disk rather than buffer in memory. Use `python-multipart`'s streaming parser or FastAPI's `UploadFile` with `.read()` in chunks:

```python
async def pool_upload(name: str, file: UploadFile = File(...)):
    dest = _pool_dest_for(name, file.filename)
    async with aiofiles.open(dest, "wb") as f:
        while True:
            chunk = await file.read(65536)
            if not chunk:
                break
            await f.write(chunk)
    # Insert pool row, return response matching legacy
```

### 5. Tests to Pass

- `render_frame_bytes_identical` — for each of the 5 JPEG fixtures, `GET /render-frame?...` and assert `response.content == fixture_bytes`. Byte-for-byte.
- `large_upload_streams` — create a 200 MB test file, POST it to `/pool/upload`, assert response 200, uploaded file exists with correct size, **server process peak RSS** during the upload stays under `baseline + 50 MB` (use `resource.getrusage` or `psutil`).
- `get_route_parity` (rendering/pool/candidates slice) — from parity fixtures
- `post_route_parity` (rendering/pool/candidates slice)

### 6. Download-preview streaming robustness

The `download_preview` handler streams generated video. Use `StreamingResponse` with an async generator that yields chunks from the encoder. Verify:
- Client sees EOF on encoder-complete.
- Server doesn't buffer the full file.
- If the client disconnects mid-stream, the encoder is torn down cleanly (no dangling ffmpeg).

This is where `graceful_shutdown_during_stream` (T65) will verify the tear-down.

---

## Verification

- [ ] All ~35 rendering/files/pool/candidates routes registered with correct operationIds
- [ ] 5 byte-identical `/render-frame` fixtures captured and pass
- [ ] 200 MB pool upload streams (RSS bounded)
- [ ] Download-preview streams without buffering
- [ ] Filmstrip, thumb, thumbnail all serve correct `Content-Type`
- [ ] Parity crawl clean across this slice
- [ ] No business logic rewritten

---

## Tests Covered

`render-frame-bytes-identical`, `large-upload-streams`, `get-route-parity` (rendering/pool/candidates slice), `post-route-parity` (rendering/pool/candidates slice).

---

## Notes

- `/render-frame` is the highest-scrutiny route in the migration. Byte-parity is non-negotiable: the frontend `<PreviewViewport>` caches these JPEGs and will display subtly different pixels if the encoder invocation changes even slightly. Use the EXACT same cv2/PIL call path the legacy handler uses. Do NOT re-encode, do NOT pass through a different JPEG writer.
- Perf baseline for `/render-frame` is captured separately in T65 — not here. Byte-parity is this task's job.
- If `pool_upload` needs a multipart streaming parser beyond what `python-multipart` + FastAPI's `UploadFile` provides, drop to Starlette's `Request.stream()` and parse manually. Document in PR.
- `GET /api/projects/{name}/staging/{stagingId}` may return binary (an intermediate render). Confirm content type and streaming shape match legacy.
