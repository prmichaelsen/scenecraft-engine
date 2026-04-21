# Backend-Rendered Preview Streaming

**Concept**: Replace the WebGL preview compositor with a Python-based backend renderer that serves scrub frames on demand and streams playback via MSE.
**Created**: 2026-04-19
**Status**: Design Specification

---

## Overview

The browser preview in scenecraft is currently rendered client-side via a WebGL shader that composites multiple video tracks, applies per-layer effects (blend modes, color grading, chroma key, strobe, adjustment layers), and drives playback. Under heavy editing loads — many overlay tracks, strobe effects across them, adjustment layers — the shader hits performance/memory ceilings that cause stutter, compile failures, and correctness bugs.

This design replaces that entire preview stack with a backend-rendered pipeline that reuses the existing Python compositor (`src/scenecraft/render/narrative.py`) which already produces the final export. Two consumption paths share one underlying frame renderer:

- **Scrub/paused**: on-demand single-frame HTTP endpoint returning JPEG bytes
- **Playback**: MSE-fed `<video>` element consuming pre-rendered fMP4 fragments

The frontend swaps between a `<canvas>` (for scrub blits) and a `<video>` (for MSE playback) inside a single `<PreviewViewport>` component. WebGL is removed entirely.

---

## Problem Statement

The WebGL preview fails in ways that can't be fixed incrementally:

1. **Performance ceiling**: too many overlay tracks cause the shader to stutter or fail to compile. The shader's per-layer loop grows with track count and eventually exceeds driver limits or hits VRAM pressure.
2. **Shader complexity is unmaintainable**: multi-layer + chroma key + strobe + blend modes + adjustment layers + color grading compose into a pipeline that's increasingly fragile to extend.
3. **Specific compositing bugs in preview only** — all work correctly in the final backend render:
   - Strobe timing desyncs across overlay tracks
   - Adjustment layers don't composite correctly
   - Chroma key spill suppression is broken
4. **Duplication**: the backend export path already correctly handles all these features. Two compositors (one correct, one buggy) is worse than one.

Migrating the preview to the backend renderer **inherently fixes the preview-only bugs** (strobe, adjustment layers, chroma spill) because the preview starts using the same code path the final render already uses.

Pain points that are NOT drivers (per clarification-6):
- Cross-browser: works
- Blend modes + brightness/contrast/exposure: no preview-vs-render divergence

The performance ceiling is the primary driver; eliminating the preview-only compositing bugs is a major secondary benefit.

---

## Solution

### High-level architecture

```
                      ┌─────────────────────────┐
                      │   narrative.py          │
                      │                         │
   build_schedule ───▶│  Schedule               │
   (DB + meta)        │  (segments, effects,    │
                      │   track stack)          │
                      │                         │
                      │  render_frame_at(t) ────┼─▶ np.ndarray (BGR)
                      └─────────────────────────┘
                                  │
                ┌─────────────────┴──────────────────┐
                │                                    │
                ▼                                    ▼
      ┌──────────────────┐              ┌─────────────────────────┐
      │ GET /render-frame│              │ Background render loop  │
      │   ?t=X           │              │ encodes H.264 via PyAV, │
      │   → JPEG         │              │ emits fMP4 fragments,   │
      │                  │              │ pushes to WS channel    │
      └──────┬───────────┘              └──────────┬──────────────┘
             │                                     │
             ▼                                     ▼
      ┌──────────────────┐              ┌─────────────────────────┐
      │ <canvas>         │              │ <video> + MediaSource   │
      │ (scrub/paused)   │              │ (playback)              │
      └──────────────────┘              └─────────────────────────┘
             └────────────── <PreviewViewport> ──────────────┘
                              (z-index swap on play state)
```

### Core primitive: `render_frame_at(schedule, t) -> np.ndarray`

Extract the per-frame body of `assemble_final`'s main loop into a pure function that takes a pre-built `Schedule` and a timeline time, and returns the composited frame. `assemble_final` becomes a loop over `render_frame_at`.

### Two consumption paths

**Scrub** (`GET /api/projects/:name/render-frame?t=12.3`):
- Returns JPEG bytes (q=85)
- Optional `?quality=` (percentage) for preview resolution (default preview = 960x540, full = project resolution)
- L1 memory cache + L2 disk cache, both keyed on `(time_ms, project_version, params_hash)`
- Fine-grained invalidation: each DB write evicts only frames in the affected time range

**Playback** (MSE):
- Server maintains a per-session `RenderWorker` that pre-renders 10s ahead of the playhead
- Encodes to H.264 via PyAV (ultrafast preset), emits ~1-second fMP4 fragments
- Fragments streamed to frontend over WebSocket; frontend appends to `MediaSource → SourceBuffer`
- `<video>` element plays naturally

### Frontend: `<PreviewViewport>`

Single visible region with two stacked children:
- `<video>` — z-index toggled up when playing, bound to MSE SourceBuffer
- `<canvas>` — z-index toggled up when paused, blitting JPEG scrub frames via `ctx.drawImage(bitmap, 0, 0)`

Scrub request queue uses "latest wins": if newer request arrives before previous completes, previous is dropped.

Fallback on render failure/timeout: show last-known-good frame.

---

## Implementation

### File layout

New concerns get new files. `narrative.py` (currently 2000+ LOC) keeps only `assemble_final` and its helpers; everything else moves.

**Backend (Python):**
```
src/scenecraft/render/
├── narrative.py          existing; slim down — keep assemble_final, ffmpeg_writer, mux_audio
├── schedule.py           NEW — Schedule dataclass, build_schedule()
├── compositor.py         NEW — render_frame_at() and its inner helpers (transform, blend, grading, effects application)
├── frame_cache.py        NEW — FrameCache (L1 memory + L2 disk), invalidate()
├── preview_worker.py     NEW — RenderWorker, RenderCoordinator, latest-wins queue
└── preview_stream.py     NEW — PyAV H.264 encode loop, fMP4 fragmenter

src/scenecraft/
├── api_server.py         existing; add routes — routes call into preview_worker/frame_cache
└── render_api.py         NEW — GET /render-frame handler, WS /preview-stream handler (split out of api_server.py for clarity)
```

**Frontend (TypeScript):**
```
src/components/editor/
├── PreviewViewport.tsx         NEW — the <video>+<canvas> surface with z-index swap
└── PreviewPanel.tsx            existing; swap WebGL canvas for <PreviewViewport>

src/hooks/
├── useMSEPlayback.ts           NEW — MediaSource + SourceBuffer + WS plumbing
└── useLatestWinsRequest.ts     NEW — scrub request queue

src/lib/
└── preview-client.ts           NEW — fetchScrubFrame(), openPreviewStream()
```

**Deletions in the WebGL-removal PR:**
- `src/components/editor/BeatEffectPreview.tsx` — WebGL shader + per-layer uniform management
- Any WebGL-specific helpers it pulls in (shader source, framebuffer utils, texture loaders)

### Refactor `narrative.py:assemble_final`

**Target decomposition** (split across `schedule.py`, `compositor.py`, and the slimmed `narrative.py`):

```python
# src/scenecraft/render/schedule.py

@dataclass
class Schedule:
    """Result of build_schedule — all state needed to render any frame in [0, duration]."""
    segments: list[Segment]              # base track + overlay track clip schedules
    effects: list[EffectEvent]            # beat effects timeline
    settings: dict                        # from meta
    fps: float
    duration: float
    source_handles: dict[str, cv2.VideoCapture]  # opened once, reused per frame

def build_schedule(work_dir: Path, session_dir: Path | None = None) -> Schedule:
    """Build the per-session render schedule from DB + filesystem. Expensive; call once per session."""


# src/scenecraft/render/compositor.py

def render_frame_at(schedule: Schedule, t: float) -> np.ndarray:
    """Render a single composited BGR frame at time t. Pure function of (schedule, t)."""


# src/scenecraft/render/narrative.py (slimmed)

from scenecraft.render.schedule import build_schedule
from scenecraft.render.compositor import render_frame_at

def assemble_final(yaml_path: str, output_path: str, ...) -> str:
    """Full-timeline render. Now just: build_schedule + loop render_frame_at + mux audio."""
    schedule = build_schedule(Path(yaml_path).parent)
    with ffmpeg_writer(output_path, schedule) as writer:
        for t in frange(0, schedule.duration, 1 / schedule.fps):
            writer.write(render_frame_at(schedule, t))
        writer.mux_audio(...)
```

**Parity test (mandatory before any further PRs):**

```python
def test_render_frame_at_parity(project_fixture):
    schedule = build_schedule(project_fixture)
    full_frames = list(_render_all_frames_via_assemble_final(project_fixture))
    for i, expected in enumerate(full_frames):
        t = i / schedule.fps
        actual = render_frame_at(schedule, t)
        assert np.array_equal(actual, expected), f"parity broke at frame {i} (t={t:.3f}s)"
```

Covers at minimum: base track, base+one overlay with a blend mode, adjustment layer, strobe effect on overlay, color grading with curves.

#### Frame cache (`src/scenecraft/render/frame_cache.py`)

Two-tier, per-session:

- **L1 (memory)**: LRU `dict[cache_key, np.ndarray]`, capped at 500 frames OR 250 MB (whichever first)
- **L2 (disk)**: `{session_dir}/frame_cache/{hash}.jpg`, capped at 10,000 frames OR 10 GB per project
  - L2 eviction runs on a background timer, not every write

Cache key: `(time_ms, project_version, params_hash)`
- `project_version` bumped on any DB write via a monotonic counter in `meta`
- `params_hash` covers track list, effect params, transform/color curves — derived from `Schedule`

#### Fine-grained invalidation (`invalidate()` in `frame_cache.py`)

Each API write endpoint calls `invalidate(session_dir, from_ts, to_ts)` with the affected time range:

| Edit type | Affected range |
|---|---|
| Insert/update/delete transition | `[from_kf.timestamp, to_kf.timestamp]` |
| Move/retime keyframe | union of incoming + outgoing transition ranges |
| Update keyframe props (selected, prompt) | referencing transition's range |
| Update track enabled/blend_mode/base_opacity | track's total transition coverage |
| Add/remove/reorder tracks | `[0, duration]` (full flush — affects z-order) |
| Update transition_effects, curves, transforms | parent transition's range |
| Update settings.json (preview_quality, etc.) | full flush |

L1 eviction is synchronous. L2 eviction is async.

#### Per-session render worker (`src/scenecraft/render/preview_worker.py`)

```python
# imports from compositor.py, schedule.py, frame_cache.py, preview_stream.py
class RenderWorker:
    """One per active session. Owns its Schedule and cache."""
    def __init__(self, session_dir: Path):
        self.schedule = build_schedule(session_dir)
        self.cache = FrameCache(session_dir)
        self.queue = LatestWinsQueue()  # scrub requests

    def render_scrub(self, t: float, quality: int) -> bytes:
        frame = self.cache.get(t) or self._render_and_cache(t)
        return encode_jpeg(frame, quality)

    def render_playback_fragment(self, from_t: float, duration: float) -> bytes:
        # Render range, encode as fMP4 via PyAV (delegates to preview_stream.py)
        ...

class RenderCoordinator:
    """Global: caps total concurrent workers at cpu_count - 1."""
    def get_worker(self, user: str, project: str) -> RenderWorker: ...
    def evict_idle(self, idle_timeout=300): ...
```

Spawned lazily on first request; torn down after 5 min idle.

#### MSE encoding (`src/scenecraft/render/preview_stream.py`)

PyAV-based H.264 encoder wrapper. `RenderWorker.render_playback_fragment()` delegates here to keep the worker file small and the encoder testable in isolation.

```python
class FragmentEncoder:
    def __init__(self, width: int, height: int, fps: float): ...
    def encode_range(self, frames: Iterable[np.ndarray]) -> bytes:
        """Encode a sequence of BGR frames → fMP4 bytes (init segment + media segment)."""
```

#### API endpoints (`src/scenecraft/render_api.py`)

Separate file from `api_server.py` to keep the growing API server from blowing past 7000 LOC. `api_server.py` just registers the render routes.

```
GET  /api/projects/:name/render-frame?t=<float>&quality=<0-100>
  → image/jpeg bytes

GET  /api/projects/:name/preview-stream  (WS upgrade)
  → bi-directional: client sends {action: "play", t: 0} / {action: "pause"} / {action: "seek", t: X}
  → server streams fMP4 fragments as binary messages
```

### Frontend

#### `<PreviewViewport>` component

```tsx
function PreviewViewport({ projectName, playing, currentTime }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [scrubBitmap, setScrubBitmap] = useState<ImageBitmap | null>(null)
  const scrubQueue = useLatestWinsRequest()  // from 2.4

  // Scrub: when paused, request frame at currentTime
  useEffect(() => {
    if (playing) return
    scrubQueue.request(currentTime, async (t) => {
      const bmp = await fetchScrubFrame(projectName, t)
      setScrubBitmap(bmp)
    })
  }, [playing, currentTime])

  // Paint canvas on bitmap change
  useEffect(() => {
    if (!scrubBitmap || !canvasRef.current) return
    const ctx = canvasRef.current.getContext('2d')
    ctx?.drawImage(scrubBitmap, 0, 0)
  }, [scrubBitmap])

  // Playback: wire MSE when playing
  useMSEPlayback(videoRef, projectName, playing, currentTime)

  return (
    <div className="preview-viewport">
      <video ref={videoRef} className={`layer ${playing ? 'on-top' : ''}`} />
      <canvas ref={canvasRef} className={`layer ${playing ? '' : 'on-top'}`} />
    </div>
  )
}
```

#### MSE playback hook

```tsx
function useMSEPlayback(videoRef, projectName, playing, seekTo) {
  useEffect(() => {
    if (!playing) return
    const ms = new MediaSource()
    videoRef.current.src = URL.createObjectURL(ms)
    ms.addEventListener('sourceopen', async () => {
      const sb = ms.addSourceBuffer('video/mp4; codecs="avc1.42E01E"')
      const ws = new WebSocket(`${WS_URL}/api/projects/${projectName}/preview-stream`)
      ws.binaryType = 'arraybuffer'
      ws.onopen = () => ws.send(JSON.stringify({ action: 'play', t: seekTo }))
      ws.onmessage = (ev) => sb.appendBuffer(new Uint8Array(ev.data))
    })
    return () => ms.endOfStream?.()
  }, [playing])
}
```

#### Latest-wins scrub queue

```tsx
function useLatestWinsRequest() {
  const pending = useRef<{ t: number; controller: AbortController } | null>(null)
  return {
    request(t: number, fn: (t: number) => Promise<void>) {
      pending.current?.controller.abort()
      const controller = new AbortController()
      pending.current = { t, controller }
      fn(t).finally(() => { if (pending.current?.t === t) pending.current = null })
    }
  }
}
```

#### `PreviewPanel` integration

`PreviewPanel.tsx` in the frontend (`scenecraft/src/components/editor/PreviewPanel.tsx`) is what currently hosts `<BeatEffectPreview>`. The migration replaces the WebGL preview *inside* the panel; the panel's other responsibilities stay.

**Stays:**

- The outer aspect-ratio container (`<div className="h-full aspect-video bg-gray-800 rounded overflow-hidden relative">`) with its `ref` for transform-handle positioning.
- **Hover overlays** rendered above the preview:
  - `hoverPreviewUrl` — floating image/video thumbnail when user hovers a candidate elsewhere in the UI.
  - `hoverVideo` — floating `<video>` overlay for transition-video hover (supports both scrub-mode and auto-play-mode).
  Both read from `usePreview()` context. They render as siblings on `z-10` above the preview layer — orthogonal to what's underneath, so the migration doesn't touch them.
- `<TransformHandles>` overlay when a transition is selected — draggable curve pins, anchor, and mask handles. Computes positions from `currentTime`. Works over any underlying surface, so unaffected.

**Changes:**

1. **Swap `<BeatEffectPreview>` for `<PreviewViewport>`**:

```tsx
// Before
{currentKeyframe?.hasSelectedImage || crossfadeData.frameA ? (
  <BeatEffectPreview
    ref={previewRef}
    src={…}
    beats={data.beats}
    audioEvents={data.audioEvents}
    userEffects={data.userEffects}
    suppressions={data.beatSuppressions}
    currentTime={currentTime}
    isPlaying={isPlaying}
    canvasWidth={canvasWidth}
    canvasHeight={canvasHeight}
    transitionFrameA={crossfadeData.frameA}
    transitionFrameB={crossfadeData.frameB}
    blendFactor={crossfadeData.blendFactor}
    layers={trackLayers.length > 0 ? trackLayers : undefined}
  />
) : (
  <div className="…">No image</div>
)}

// After
<PreviewViewport
  ref={previewRef}
  projectName={data.projectName}
  currentTime={currentTime}
  playing={isPlaying}
/>
```

The backend decides "no content" (returns 404), so the frontend's `currentKeyframe?.hasSelectedImage` check goes away. `<PreviewViewport>` handles its own empty state.

2. **Props that go away:** `src`, `beats`, `audioEvents`, `userEffects`, `suppressions`, `canvasWidth`, `canvasHeight`, `transitionFrameA`, `transitionFrameB`, `blendFactor`, `layers`. These were feeding shader uniforms and inline compositing; they're the backend's job now.

3. **`PreviewContext` shrinks.** These fields become dead weight and are removed:
   - `crossfadeData` (frameA/frameB/blendFactor)
   - `trackLayers`
   - `isTransitionLoading` (replaced by `<PreviewViewport>`'s internal scrub-loading state)
   - `updatePreview(...)` callback
   
   These fields are currently **pushed up from `Timeline`** via `updatePreview()`. The Timeline code that computes them — `trackLayers = useMemo(() => ...)` building `TrackLayer[]` from track data + frame cache, plus the transition-frame preloader that feeds `crossfadeData` — also becomes dead code and is deleted.
   
   Kept in `PreviewContext`: `hoverPreviewUrl`, `hoverVideo`, `previewRef`.

4. **Frame preloading pipeline deletes.** `scenecraft/src/lib/frame-cache.ts` (frontend — not the backend cache we just built) currently preloads keyframe images + transition video frames around the playhead. `<PreviewViewport>` doesn't need it; backend rendering + its own L1 cache covers both scrub and playback. Delete `frame-cache.ts` and the `preloadTransition`/`preloadKeyframeImage` call sites in `Timeline.tsx` and `BinPanel.tsx`.

5. **`previewRef` handle narrows.** Today:
   ```ts
   export type BeatEffectPreviewHandle = { getCanvas: () => HTMLCanvasElement | null }
   ```
   Used in one place: `Timeline.tsx`'s `recordPreview(canvas, audio, …)` call, which calls `canvas.captureStream(24)` for `MediaRecorder` → WebM download.
   
   In the new world, `<PreviewViewport>` has a `<canvas>` and a `<video>` child, alternating z-index. Two paths for the download feature:
   
   - **Near-term (in the WebGL-removal PR): expose both**
     ```ts
     export type PreviewViewportHandle = {
       getCanvas: () => HTMLCanvasElement | null
       getVideo: () => HTMLVideoElement | null
     }
     ```
     Recorder picks based on `isPlaying`: `captureStream` the `<video>` during playback, the `<canvas>` during scrub. Works out of the box — both DOM elements expose `.captureStream()`.
   
   - **Longer-term: server-side range export.** Add `GET /api/projects/:name/render-range?start=X&end=Y&format=webm` that renders the range on the backend and streams the file. Frontend just downloads it. Removes the `MediaRecorder` dance entirely and guarantees the recording matches the final export. Track as a follow-up; not blocking for the migration PR.

6. **"No image" placeholder.** Currently shown when `!currentKeyframe?.hasSelectedImage && !crossfadeData.frameA`. After migration, `<PreviewViewport>` should show its own equivalent when the backend returns `404 NO_CONTENT` — a small "Add a keyframe to see a preview" state rendered in the canvas area. Internal state, no external prop.

#### Files touched in PR 4 (Frontend `<PreviewViewport>`)

Create:
- `src/components/editor/PreviewViewport.tsx`
- `src/hooks/useMSEPlayback.ts`
- `src/hooks/useLatestWinsRequest.ts`
- `src/lib/preview-client.ts` — `fetchScrubFrame(project, t, quality?)`, `openPreviewStream(project)`

Modify:
- `src/components/editor/PreviewPanel.tsx` — swap `<BeatEffectPreview>` for `<PreviewViewport>`; keep hover overlays and `<TransformHandles>`.
- `src/components/editor/PreviewContext.tsx` — remove `crossfadeData`, `trackLayers`, `isTransitionLoading`, `updatePreview`; keep `hoverPreviewUrl`, `hoverVideo`, `previewRef`. Update `BeatEffectPreviewHandle` → `PreviewViewportHandle`.
- `src/components/editor/Timeline.tsx` — delete the `trackLayers` memo, the transition-frame preloader, and the `updatePreview(...)` call. Keep currentTime/isPlaying state.
- `src/lib/preview-recorder.ts` — update to accept either canvas or video element (per point 5 above).

#### WebGL removal

All WebGL shader code, textures, framebuffer management, and per-layer compositing logic in the frontend is deleted in the same PR that introduces `<PreviewViewport>`. No feature flag, no fallback code path. `git revert` is the rollback.

Files deleted:
- `src/components/editor/BeatEffectPreview.tsx`
- `src/lib/frame-cache.ts` (see PreviewPanel integration, point 4)
- Any WebGL-specific helpers these pull in (shader constants, framebuffer utils, texture loaders) — audit via `grep WebGLRenderingContext src/`.

---

## Benefits

- **Single compositor**: one render path for preview and final export. Correctness fixes land in one place.
- **Removes the perf ceiling**: CPU-bound rendering doesn't have WebGL's shader-complexity or VRAM limits; track count scales linearly with compositor cost, not quadratically with shader state.
- **Correct strobe + adjustment layers**: backend already handles these correctly; no more shader-vs-Python divergence.
- **Simpler frontend**: `<PreviewViewport>` is ~200 LOC of plumbing vs. the ~1500 LOC of shader + uniforms + layer management being removed.
- **Streaming infrastructure**: MSE playback gives us a foundation for remote editing (cloud render → browser) later with no protocol change.

---

## Trade-offs

- **Server load**: rendering is now on the server, not distributed to each user's GPU. At `cpu_count - 1` concurrent workers per server, multi-user hosts need more CPU budget. Mitigation: per-session serialization + LRU cache hit rate > 80% on typical edit sessions.
- **Scrub latency on cold frames**: cached frames serve in ~5ms; cold frames take `1/compositor_fps` seconds (~66-200ms at 5-15 fps). WebGL was always instant regardless. Mitigation: pre-render-ahead buffer warms cache as user works.
- **Playback latency**: MSE adds ~200-500ms fragment buffer. WebGL had frame-by-frame latency. Mitigation: acceptable for editor-style playback (not real-time interactive).
- **Invalidation complexity**: fine-grained range eviction is ~200 LOC over a coarse "bump version counter" approach. Worth it — cache hit rate during editing is vastly better.
- **Refactor risk**: `assemble_final` refactor touches the critical export path. Mitigation: parity tests must pass before any downstream PRs merge.

---

## Dependencies

### Python (backend)

- `opencv-python` — already used
- `numpy` — already used
- `av` (PyAV) — **new**, for H.264 encoding and fMP4 muxing (`pip install av`)
- `websockets` — already used (ws_server.py)

### Frontend

- No new npm deps. MSE and `<video>`/`<canvas>` are native browser APIs.

### Infrastructure

- Server CPU headroom for render workers. At 1080p30, each worker needs ~1 core for the compositor + ~0.5 core for H.264 encode = ~1.5 cores under load. Plan capacity accordingly.

---

## Testing Strategy

### Parity (gate before any endpoint work)

`test_render_frame_at_parity` — runs `assemble_final` end-to-end, then calls `render_frame_at(schedule, t)` for every timeline frame, asserts `np.array_equal` on each. Must pass on:
- Project with base track only
- Project with base + overlay (blend mode: multiply)
- Project with adjustment layer on top
- Project with strobe effect on overlay track
- Project with color grading curves (brightness/contrast/exposure)
- Project with chroma key overlay

### Integration

- `render-frame` endpoint returns valid JPEG that matches the numpy buffer from `render_frame_at`
- Cache invalidation: mutate a transition, then request a frame in its range — cache miss; request outside range — cache hit
- MSE playback: pre-render fragment for time range, verify `<video>` plays it; simulate edit in range, verify playback stutters/re-renders correctly

### Performance benchmarks

- `render_frame_at` sustained fps at 1080p on test host — target ≥5 fps
- Scrub latency p50/p95: target p50 < 20ms (cached), p95 < 500ms (cold) at preview resolution
- Memory: L1 cache should evict correctly at the 500-frame / 250-MB cap

---

## Migration Path

This is greenfield (no production users) and `--no-flag`. Single large PR series, no incremental rollout gate.

1. **PR 1 — `assemble_final` refactor + parity tests**: extract `build_schedule` and `render_frame_at`. Parity test suite added. `assemble_final` behavior byte-identical. No new endpoints.
2. **PR 2 — Scrub endpoint + cache + invalidation**: `GET /render-frame`, L1/L2 cache, fine-grained invalidation wired into all write endpoints.
3. **PR 3 — MSE playback**: `RenderWorker`, `RenderCoordinator`, `/preview-stream` WS, PyAV encode loop, fMP4 fragmentation.
4. **PR 4 — Frontend `<PreviewViewport>`**: video + canvas swap, scrub queue, MSE hook. WebGL code still present but unused.
5. **PR 5 — WebGL removal + end-to-end tests**: delete shader, compositor helpers, WebGL-specific state. Add integration tests. Ship.

Estimated total: 11-18 days (2-3 weeks).

---

## Key Design Decisions

### Motivation & Scope

| Decision | Choice | Rationale |
|---|---|---|
| Primary driver | Performance ceiling (not correctness) | WebGL can't keep up with complex multi-layer compositing; compositor bugs are secondary |
| Compositing priority | Higher than other concerns | User-confirmed: "both true, compositing higher priority" |
| Migration scope | Full replacement | Performance ceiling hits on playback, so coexist doesn't solve the real problem |
| Feature flag | None | Greenfield, pre-launch; `git revert` is the rollback |

### Endpoint & Formats

| Decision | Choice | Rationale |
|---|---|---|
| Scrub endpoint | `GET /api/projects/:name/render-frame?t=X` | Simple request/response, low latency for on-demand single frames |
| Scrub image format | JPEG q=85 | Fastest encode (libjpeg-turbo ~3ms), universal browser support, ~150-300KB per frame |
| Quality override | `?quality=` param | Supports preview-resolution scrub for snappier interaction |
| Default render resolution | 960x540 (preview) | Scrub doesn't need project-output resolution; 4x pixel savings |
| Playback protocol | MSE with fMP4 fragments | Efficient bandwidth (~5-15 Mbps), natural `<video>` integration, future-proof over MJPEG |
| Streaming not needed for scrub | Single-frame request/response | Encoding a JPEG is <10ms; streaming is only beneficial for sequences |

### Cache

| Decision | Choice | Rationale |
|---|---|---|
| Cache strategy | Both tiers: L1 memory + L2 disk | L1 for hot scrub, L2 for survival across restarts |
| Cache size limits | N frames AND N bytes (both caps) | Protects against both small-frame flooding and single huge-frame blowups |
| Invalidation | Fine-grained by affected time range | Cache hit rate during editing vastly better than coarse flush |
| Cache scope | Per-user / per-session | Aligns with existing VCS session model — diverging branches have diverging frames |

### Playback & Pre-Render

| Decision | Choice | Rationale |
|---|---|---|
| Pre-render buffer | 10 seconds ahead of playhead | Smoothes over most hiccups; dial down if memory/invalidation cost shows |
| Playback invalidation behavior | Stutter / show last good frame | Avoids jarring pause; user chose this explicitly |
| Playback required in MVP | Yes | Full WebGL replacement means playback failures have no fallback |

### Performance & Concurrency

| Decision | Choice | Rationale |
|---|---|---|
| Compute backend | CPU-only | Remote host has no GPU |
| GPU path | Out of scope | Deferred optimization |
| Target scrub fps | 24+ | Achievable via cache; honest compositor throughput during cold scrubbing |
| Scrub semantics | Latest-wins request queue | Cursor tracks user precisely; no stale frame backlog |
| Concurrency model | Serialize per user, parallel across users | Matches existing session model; bounded at `cpu_count - 1` |

### Frontend

| Decision | Choice | Rationale |
|---|---|---|
| Preview surface | Single `<PreviewViewport>` with `<video>` + `<canvas>` children | Each mode uses the tech best suited; mirrors Premiere/DaVinci internals |
| Scrub request cadence | Per-frame-drag (relying on cache) | Responsive; cache should hide most costs |
| Render failure fallback | Last-known-good frame | Cleanest user-visible behavior |

### Refactor & Parity

| Decision | Choice | Rationale |
|---|---|---|
| Refactor shape | Extract `render_frame_at`; `assemble_final` becomes a loop | Unified single implementation; no duplication |
| Pixel parity | Identical pixels required pre-encode | Preview and final render share the compositor; parity tests lock this in |
| Preview-resolution parity | Lanczos downscale applied deterministically after compositing | Keeps parity guarantee on the render step |

### Migration

| Decision | Choice | Rationale |
|---|---|---|
| Delivery approach | Feature-complete, no phased rollout | Greenfield, no users to protect; user explicitly rejected phasing |
| WebGL removal | Deleted in the same PR series that introduces backend rendering | No fallback code path to maintain |
| Estimated duration | 2-3 weeks (11-18 days) | Covers all 5 PRs: refactor, endpoint+cache, MSE playback, frontend viewport, WebGL removal |

---

## Future Considerations

- **GPU-accelerated compositor**: add `cupy` or `torch` path for sites that have GPUs; wrap behind `SCENECRAFT_GPU_RENDER=1`. Only worth doing after the CPU path proves stable and the perf floor bites a real user.
- **Remote rendering**: MSE streaming means the renderer doesn't need to be on the same machine as the browser. Natural extension for cloud-hosted editing.
- **Truncating invalidation on playback buffer**: when an edit hits the pre-rendered buffer, keep the unaffected prefix (already rendered) and re-render only from the edit point forward. Reduces stutter window.
- **WebCodecs on the frontend**: if browsers ship reliable WebCodecs support, we could decode H.264 fragments in a worker thread instead of relying on `<video>` — tighter playback control but more bespoke.
- **Scene-aware invalidation**: once scenes/characters/settings (clarification-3) ship, edits to scene-level prompts could skip frame-cache invalidation since they don't affect rendered pixels.

---

**Status**: Design Specification
**Recommendation**: Implement the 5-PR migration. Start with the `assemble_final` refactor + parity tests; downstream PRs cannot begin until parity passes.
**Related Documents**: [clarification-6-backend-rendered-preview-streaming.md](../clarifications/clarification-6-backend-rendered-preview-streaming.md)
