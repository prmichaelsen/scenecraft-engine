# Task 44: Chunked proxies (split large sources into time-windowed proxy files)

**Milestone**: [M12 - NLE-Style Preview Rendering Pipeline](../../milestones/milestone-12-nle-preview-pipeline.md)
**Design Reference**: None
**Estimated Time**: 1-2 days
**Dependencies**: Task 39 (proxy generation) — this extends the same infra
**Status**: Completed
**Repository**: `scenecraft-engine`

---

## Objective

Instead of one giant proxy file per source (e.g. 2.4GB for a 2.4h 1080p source), generate N smaller chunk proxies along the source's timeline (e.g. one per 5 minutes). Each chunk is a self-contained fMP4 with its own keyframes. The compositor transparently maps timeline-time → `(chunk_index, chunk_local_time)` and opens only the chunk(s) the playhead actually needs.

To the user: still "one clip" on the timeline. Under the hood: cheaper opens, cheaper seeks, better OS page-cache behavior.

---

## Context

Task 39 introduces proxies as a single file per source. That already cuts decode CPU ~4x for 1080p → 540p. But large single-file proxies still have I/O friction:

- `cv2.set(CAP_PROP_POS_FRAMES, N)` on a 2.4GB file walks a long index before reaching a keyframe near N
- 16 parallel render threads all reading from a single file compete for page-cache blocks
- Scrub HTTP path (random-access by nature) pays the full seek cost on every request
- Storage for one monolithic file is less friendly to sync/backup tools

Chunking addresses the I/O dimension that proxies don't. The two compose cleanly: chunked proxies give you small-file decode AND low pixel count at the same time.

The original idea from chat was to chunk imports at arrival time. Chunking the proxy instead is equivalent in end-user effect and cheaper to implement:
- No disruption to existing import/pool paths
- Proxy generator owns the chunking logic in one place
- Originals stay untouched — export path unaffected

---

## Steps

### 1. Chunk layout on disk

```
{project}/proxies/{hash}/
  manifest.json                       # {source_path, source_mtime_ns, chunk_seconds, chunk_count, total_seconds}
  chunk-000.mp4                       # 0..chunk_seconds of source
  chunk-001.mp4                       # chunk_seconds..2*chunk_seconds
  ...
  chunk-NNN.mp4                       # last partial chunk
```

`{hash}` same format as task-39 (SHA-256 of source path + mtime_ns, truncated). Using a directory per source instead of a single file.

Default chunk duration: **300 seconds (5 minutes)**. Configurable via `DEFAULT_PROXY_CHUNK_SECONDS`.

### 2. Chunked generation command

Single `ffmpeg` invocation with the segment muxer:

```
ffmpeg -hide_banner -loglevel error -y \
  -i <source> \
  -vf scale=-2:540 \
  -c:v libx264 -preset faster -crf 28 -pix_fmt yuv420p -an \
  -f segment \
  -segment_time 300 \
  -reset_timestamps 1 \
  -segment_list <tmp_dir>/chunks.txt \
  <tmp_dir>/chunk-%03d.mp4
```

Notes:
- `-reset_timestamps 1` makes each chunk start at t=0 → chunk-local time == absolute time within that chunk. Simplifies the compositor's mapping.
- `-segment_time 300` targets 300s chunks; actual duration honors keyframes (chunks cut only on IDR frames, so real durations are slightly variable — manifest records actuals).
- Segment list written to `chunks.txt` — we parse it to build the manifest.

### 3. Manifest schema

```json
{
  "version": 1,
  "source_path": "/abs/path/to/source.mp4",
  "source_mtime_ns": 1735689600000000000,
  "chunk_seconds": 300.0,
  "total_seconds": 8678.17,
  "chunks": [
    {"index": 0, "file": "chunk-000.mp4", "start": 0.0,    "end": 301.2},
    {"index": 1, "file": "chunk-001.mp4", "start": 301.2,  "end": 602.4},
    ...
  ]
}
```

Manifest is the source of truth for compositor mapping. Chunk boundaries may not be exact multiples of `chunk_seconds` due to keyframe alignment — `start`/`end` record actual GOP-aligned boundaries.

### 4. Extend `proxy_generator.py`

New API:

```python
def chunked_proxy_dir_for(project_dir: Path, source_path: str) -> Path | None: ...
def chunked_proxy_manifest(project_dir: Path, source_path: str) -> Manifest | None: ...
def chunk_for_time(manifest: Manifest, t: float) -> tuple[int, float] | None:
    """Returns (chunk_index, time_within_chunk). Returns None if t is past the end."""

def generate_chunked_proxy(
    project_dir: Path, source_path: str,
    target_height: int = 540,
    chunk_seconds: float = 300.0,
) -> Manifest | None: ...
```

Behavior:
- `generate_chunked_proxy` transcodes into a tmp dir, writes manifest last, then atomically renames to final location. Proxy is "ready" iff the manifest file exists.
- `ProxyCoordinator.ensure_proxy(project, source, mode='chunked')` routes to chunked generator; `mode='single'` stays as-is for small sources.

### 5. Compositor integration

`_resolve_source_for_read` currently returns a single file path. For chunked proxies it needs to return `(file_path, time_offset)` or equivalent — the cursor math in `_get_frame_at` lives at the file level, not the source-timeline level.

Proposed signature change: `_resolve_source_for_read(...) -> tuple[str, float]` where the float is the time-within-file offset to seek to (0.0 for single-file, chunk-start-time for chunked). `_get_frame_at` computes `idx_in_file = int((t_within_source - chunk_start) * fps)` and uses that for `cap.set(CAP_PROP_POS_FRAMES, ...)`.

The `stream_caps` cache key becomes `(seg_idx, chunk_file_path)` — each chunk gets its own long-lived cap.

### 6. Sensible defaults per source size

- Source duration < chunk_seconds → single-file proxy (fallback to task-39 behavior)
- Source duration ≥ chunk_seconds → chunked

So short sources pay no overhead; long sources get the chunking benefit.

### 7. Invalidation

Same mtime-based invalidation as task-39. The `{hash}` directory name incorporates source mtime, so a changed source generates a new directory on next `ensure_proxy`. Old directories can be GC'd by a later sweep pass.

### 8. Tests

- `generate_chunked_proxy` produces N chunks whose start/end cover the full source duration
- `chunk_for_time` returns correct (index, offset) for boundary and mid-chunk timestamps
- Scrub at t=1500 (chunk 5) opens only `chunk-005.mp4`, not any other chunk
- Playback across a chunk boundary: seamless — no visible glitch at transition
- Corrupt manifest → `chunked_proxy_manifest` returns None, falls back to single-file or original

---

## Verification

- [ ] Chunked proxy generation for the 2.4h oktoberfest source produces ~29 chunks (~5 min each) in under 10 minutes
- [ ] Scrub to t=3600: opens `chunk-012.mp4`, no other chunks touched in strace
- [ ] Playback from t=2700 across the t=3000 chunk boundary: no visible stutter or frame gap in the `<video>` output
- [ ] `base_frame` phase time drops another 20-30% vs single-file 540p proxy (smaller file = faster seek + better cache locality)
- [ ] Manifest survives process restart — re-running playback uses existing chunks, doesn't regenerate

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Chunk granularity | 300s default | Balances file size (~30MB each at 540p) with chunk count (~29 for a 2.4h source) — neither so many that directory listing is slow nor so few that seeks are long |
| Reset timestamps per chunk | Yes | Chunk-local time == 0.0 at start; compositor seek math is simpler and immune to tfdt drift |
| Manifest format | JSON sidecar | Robust, human-readable, one small read per source on first touch; not SQLite (overkill) |
| Boundary alignment | GOP-aligned via ffmpeg segment muxer | Chunks are self-contained decodable units; no partial-GOP chunks |
| Short sources | Single-file proxy (task-39) | Chunking overhead not worth it for clips < `chunk_seconds` |
| Generation atomicity | Tmp dir + rename | Partial failures don't leave half-populated proxy dirs that `chunked_proxy_manifest` would mis-read |

---

## Notes

- Extends task-39 cleanly — same coordinator, same hash scheme, just "mode=chunked" instead of single-file. No disruption to already-generated task-39 proxies.
- Future: chunk boundaries could align with base-track segment boundaries in `build_schedule`. That would mean one chunk per visible timeline segment — even cheaper. But adds coupling between compositor and proxy generator. Not in scope here.
- Memory: even 29 chunks × one cv2.VideoCapture each per thread is fine — caps aren't large. The stream_caps dict holds (seg_idx, chunk_path) tuples; at 16 threads × ~2 chunks active at playhead, that's ~32 caps per worker. Totally reasonable.
- Export path: untouched. Export reads the original source, never looks at proxies (chunked or single).
