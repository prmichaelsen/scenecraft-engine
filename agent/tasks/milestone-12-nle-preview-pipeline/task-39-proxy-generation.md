# Task 39: 540p proxy generation + proxy-backed compositor read path

**Milestone**: [M12 - NLE-Style Preview Rendering Pipeline](../../milestones/milestone-12-nle-preview-pipeline.md)
**Design Reference**: None (design captured in the milestone doc)
**Estimated Time**: 1-2 days
**Dependencies**: None
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Cut preview decode cost by ~4x by generating 540p H.264 proxies for every base-track source file and routing the preview compositor to read them instead of the originals. Export path unchanged (continues to read originals).

---

## Context

The performance audit in the chat shows base-frame decode eating ~400ms per 1080p frame with warm caps (20s summed across 16 threads for 48 frames). That's cv2 H.264 decoders saturating all cores on 1080p pixels. Dropping to 540p reduces pixel count 4x → decode CPU 4x → fragment render cycle from 1.7s to ~0.4s → comfortably realtime with headroom.

Every pro NLE uses proxies. It's the standard answer to "preview-time performance is slower than realtime."

---

## Steps

### 1. Storage layout

- Proxies live at `{project_dir}/proxies/{hash}.mp4`
- `{hash}` is a truncated SHA-256 of `(absolute source path + source mtime_ns)` — changes when the source changes so we auto-invalidate
- Directory created on first proxy generation

### 2. Proxy generator module

Create `src/scenecraft/render/proxy_generator.py`:

```python
def proxy_path_for(project_dir: Path, source_path: str) -> Path:
    """Canonical proxy location for a source. Does NOT check existence."""

def proxy_exists(project_dir: Path, source_path: str) -> bool:
    """True if proxy is present AND matches current source mtime."""

def generate_proxy(project_dir: Path, source_path: str, target_height: int = 540) -> Path:
    """Synchronously transcode source to a 540p (or configured) proxy.
    Blocks until complete. Returns the proxy path."""
```

Transcode command (runs ffmpeg subprocess):
```
ffmpeg -hide_banner -loglevel error -y \
  -i <source> \
  -vf scale=-2:540 \
  -c:v libx264 -preset faster -crf 28 -pix_fmt yuv420p \
  -an \
  <proxy>
```

Options:
- `scale=-2:540` — preserve aspect ratio, even width
- `-an` — drop audio (preview doesn't need it; audio-mixer loads source directly)
- `preset=faster crf=28` — fast transcode, acceptable quality

### 3. Background proxy generator worker

- Module-global `ProxyCoordinator` (mirrors `RenderCoordinator`)
- Exposes `ensure_proxy(project_dir, source_path) -> Future[Path]`
- Caps concurrent transcodes at 2 (leaves CPU for other work)
- On ensure: if proxy exists and fresh → return immediately; else enqueue transcode

### 4. Compositor integration

In `_get_frame_at` / `_prime_segments` (`compositor.py`):
- When stream_caps mode is active, resolve the effective source path via a new helper `_effective_source_for_preview(project_dir, seg, prefer_proxy=True) -> str`
- Helper checks: does proxy exist for `seg["source"]` in `project_dir`? If yes, return proxy path. If no, kick `ProxyCoordinator.ensure_proxy` async and return original (first render falls through to original; subsequent after proxy ready uses it)
- For scrub mode and export path: always use original (quality)

### 5. Wire prefer_proxy flag

- `render_frame_at` gets a new kwarg `prefer_proxy: bool = False`
- Preview worker (`preview_worker.py`): passes `prefer_proxy=True`
- Scrub HTTP endpoint: passes `prefer_proxy=False` (scrub uses originals for accuracy)
- Export / narrative.py path: `prefer_proxy=False`

### 6. Proxy dimensions vs output dimensions

The compositor resizes to `(w, h)` from schedule anyway. So a 540p proxy still gets upscaled back to 1080p by `_get_frame_at` if the schedule says 1080p. Two approaches:
- **A**: Leave resize logic as-is — proxy decode is cheap, resize back to schedule dims. Encode output stays at 1080p. Saves decode only.
- **B**: Also lower encoder output resolution in preview mode — schedule.width/height reduced to 540p equivalents, encoder emits 540p, `<video>` upscales in browser. Saves decode + encode + network bytes.

Recommend **B** — bigger win. Add `preview_scale_factor` (default 0.5) to worker, applies to encoder + schedule dims used in the preview path.

### 7. Lazy proxy generation on play

When a preview worker spins up, scan base-track segments, call `ensure_proxy` for each. First play falls back to originals if proxies aren't ready — subsequent plays use proxies as they land.

### 8. Manual proxy generation endpoint

`POST /api/projects/:name/proxies/generate` — kicks proxy generation for all sources, returns immediately with a job ID. Optional, for UI to trigger on project load.

### 9. Tests

- `tests/test_proxy_generator.py`:
  - `proxy_path_for` generates stable paths for same (source, mtime) pair
  - `proxy_exists` returns False for missing, True for present, False for stale (mtime changed)
  - `generate_proxy` creates a valid 540p mp4 (assert dims via cv2 VideoCapture)

---

## Verification

- [ ] Generating proxies for the oktoberfest_show_01 project (single 2.4h 1080p source) completes in < 15 minutes
- [ ] `proxy_exists` returns True after generation
- [ ] Touching the source file makes `proxy_exists` return False (mtime invalidation)
- [ ] Playback with proxies ready: `base_frame` phase drops from ~20s summed to ~5s summed
- [ ] Scrub HTTP endpoint still reads original (serves full-quality frames)
- [ ] Export path (`assemble_final`) reads original, no proxy dependency
- [ ] Fragment cycle total drops from ~2.5s to ~1.0-1.2s with proxies + 540p encode
- [ ] Proxies directory is git-ignored (add to `.gitignore` if under tracked path)

---

## Key Design Decisions

### Model

| Decision | Choice | Rationale |
|---|---|---|
| Proxy resolution | 540p default (configurable) | 4x pixel reduction, imperceptible at preview quality |
| Proxy storage | Per-project, filesystem | Simple; no shared pool; invalidates naturally on project move |
| Proxy key | SHA-256(source_path + mtime_ns), truncated | Auto-invalidates on source change without needing watcher |
| Audio in proxy | Dropped (`-an`) | Preview doesn't use audio from video track; audio-mixer handles audio separately |
| Generation | Lazy (on demand) + background | Avoid forcing user to wait at project load |
| Export uses proxies | No | Quality matters for export; decode cost acceptable |

---

## Notes

- Proxies can be large (hundreds of MB for long sources). Consider adding a GC pass later — evict proxies for sources not touched in N days.
- If ffmpeg isn't available on the system, fail ensure_proxy gracefully and log; compositor continues with originals (no regression).
- Future: per-segment proxies (only the visible range of each base-track segment) — would cut storage and gen time further. Not in scope here.
