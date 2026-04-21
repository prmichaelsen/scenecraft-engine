# Task 34: MSE playback — RenderWorker, PyAV encoder, `/preview-stream` WebSocket

**Milestone**: [M11 - Backend-Rendered Preview Streaming](../../milestones/milestone-11-backend-rendered-preview-streaming.md)
**Design Reference**: [backend-rendered-preview-streaming](../../design/local.backend-rendered-preview-streaming.md)
**Estimated Time**: 3-5 days
**Dependencies**: Task 32 (`render_frame_at`), Task 33 (frame cache — playback can warm/use it)
**Status**: Not Started

---

## Objective

Stream continuous playback from the backend compositor to the frontend's `<video>` element via MediaSource Extensions (MSE). Server encodes frames to H.264 inside fMP4 fragments, pushes them over a WebSocket, frontend feeds them to a `SourceBuffer`.

---

## Context

Scrub is on-demand single frames. Playback is continuous — at 24fps the compositor must sustain throughput or pre-render ahead. The design commits to pre-rendering 10 seconds ahead of the playhead so compositor cold spots don't stutter playback. On any edit in the pre-rendered range, playback stutters briefly (user-approved tradeoff) while the buffer refills.

PyAV is the encoder. libx264 is in the system ffmpeg; if PyAV's bindings don't expose it cleanly, fall back to a direct `ffmpeg -` subprocess.

---

## Steps

### 1. Fragment encoder (`src/scenecraft/render/preview_stream.py`)

```python
class FragmentEncoder:
    def __init__(self, width: int, height: int, fps: float): ...
    def encode_range(self, frames: Iterable[np.ndarray]) -> tuple[bytes, bytes]:
        """Encode a sequence of BGR frames → (init_segment, media_segment) bytes.
        Init segment contains moov + codec config (sent once per connection).
        Media segment is a moof+mdat fMP4 fragment (one or more per playback range).
        """
```

- Use `av.open(bytes_io, mode='w', format='mp4')` with `movflags='+frag_keyframe+empty_moov+default_base_moof'`
- Codec: `libx264` with `preset='ultrafast', tune='zerolatency', crf=23`
- If PyAV fMP4 muxing is finicky, fall back to piping frames into `ffmpeg` subprocess

### 2. Render worker (`src/scenecraft/render/preview_worker.py`)

```python
class RenderWorker:
    """One per active session. Owns its Schedule, frame cache, fragment encoder."""
    def __init__(self, project_dir: Path): ...
    def play(self, start_t: float) -> None: ...
    def seek(self, t: float) -> None: ...
    def pause(self) -> None: ...
    # Yields (init_segment, *media_segments) as rendering progresses
    def fragments(self) -> Iterator[bytes]: ...

class RenderCoordinator:
    """Caps total concurrent workers at cpu_count - 1. Lazily spawned per session."""
    def get_worker(self, project_dir: Path) -> RenderWorker: ...
    def evict_idle(self, idle_timeout=300): ...
```

- Pre-render 10 seconds ahead of the playhead in the background
- On seek: discard any queued fragments past the seek point, resume rendering from new position
- On edit invalidation (detected via `frame_cache.invalidate_project`): flush queued fragments, resume from current playhead

### 3. WebSocket endpoint (`/api/projects/:name/preview-stream`)

Bidirectional protocol:

**Client → server (JSON text frames):**
```json
{"action": "play", "t": 0.0}
{"action": "seek", "t": 12.5}
{"action": "pause"}
{"action": "stop"}
```

**Server → client (binary frames):**
- First frame: init segment (fMP4 header)
- Subsequent frames: media segments (fMP4 fragments), one per ~1-2 second of rendered content

Wired into `ws_server.py` alongside the existing chat WebSocket router. Uses the same auth (cookie/bearer) as the REST API.

### 4. Graceful shutdown

- Worker idle timeout: 5 minutes → tear down, release video file handles
- Connection close: cancel any in-flight render, release worker
- Coordinator shutdown: stop all workers on server exit

### 5. Tests (`tests/test_preview_stream.py`)

- Fragment encoder produces playable fMP4 (probe via ffmpeg to verify `moov` + `moof` structure)
- Init segment parses as valid fMP4 header
- Media segments concatenated back yield a playable MP4
- WebSocket accepts play → returns init + media segments; accepts seek → discards old fragments, emits new ones
- Coordinator caps concurrent workers
- Idle worker gets evicted after timeout
- Edit invalidation flushes pre-rendered queue

### 6. Manual smoke test

- Start the server, connect from `wscat` or a simple HTML page with `MediaSource`
- Verify `<video>` plays the streamed fMP4 fragments
- Edit a transition while playing; verify playback stutters briefly then resumes with the edit reflected

---

## Verification

- [ ] `FragmentEncoder.encode_range()` produces fMP4 bytes that pass `ffprobe -show_frames`
- [ ] Init segment contains moov atom; media segments contain moof + mdat atoms
- [ ] WebSocket handshakes and accepts `{"action": "play"}` → emits init + media
- [ ] Seek during playback replaces queued fragments
- [ ] Pause stops rendering; play resumes from last position
- [ ] Worker evicted after 5 min idle
- [ ] RenderCoordinator caps workers at `cpu_count - 1`
- [ ] Edit to a project invalidates its worker's queue; subsequent fragments reflect the edit
- [ ] All tests pass
- [ ] Manual smoke test: MSE `<video>` plays a 10-second range end-to-end

---

## Expected Output

### Files Created
- `src/scenecraft/render/preview_stream.py`
- `src/scenecraft/render/preview_worker.py`
- `tests/test_preview_stream.py`

### Files Modified
- `src/scenecraft/ws_server.py` — route `/ws/preview-stream/:name` to the preview worker protocol handler
- `src/scenecraft/render/frame_cache.py` — may need a hook so workers can detect project invalidation

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Protocol | MSE with fMP4 | Bandwidth-efficient, native `<video>` integration. MJPEG too heavy off localhost; WebRTC overkill. |
| Codec | H.264 via libx264 | Ultrafast preset runs 2-3× real-time on CPU; universal browser support |
| Pre-render buffer | 10 seconds | Smoothes most hiccups; invalidation cost acceptable per user |
| On invalidation | Stutter / last good frame | User-approved; no pause, no WebGL fallback |

---

## Common Issues and Solutions

### Issue 1: PyAV's fMP4 muxing produces invalid fragments
**Symptom**: MediaSource throws `NotSupportedError` on `appendBuffer`
**Solution**: Fall back to ffmpeg subprocess — pipe BGR frames into `ffmpeg -f rawvideo -pix_fmt bgr24 -s {w}x{h} -r {fps} -i - -c:v libx264 -preset ultrafast -tune zerolatency -movflags +frag_keyframe+empty_moov+default_base_moof -f mp4 -`

### Issue 2: Init segment sent more than once
**Symptom**: Frontend logs `SourceBuffer` quota errors
**Solution**: Track a `sent_init` flag per connection; only emit init on first `play` after connection open

### Issue 3: Seek lag feels sluggish
**Symptom**: User seeks, playback takes seconds to resume
**Solution**: Render at preset `ultrafast`. If still slow, reduce fragment size (1s → 500ms) so the first fragment after seek arrives sooner

---

**Status**: Not Started
