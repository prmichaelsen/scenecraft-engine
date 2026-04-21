# Task 35: Frontend `<PreviewViewport>` — video + canvas swap, scrub queue, MSE hook

**Milestone**: [M11 - Backend-Rendered Preview Streaming](../../milestones/milestone-11-backend-rendered-preview-streaming.md)
**Design Reference**: [backend-rendered-preview-streaming](../../design/local.backend-rendered-preview-streaming.md) (see §Frontend, §PreviewPanel integration)
**Estimated Time**: 2-3 days
**Dependencies**: Task 33 (`/render-frame` endpoint), Task 34 (`/preview-stream` WebSocket)
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Replace the WebGL preview compositor in the frontend with a single `<PreviewViewport>` component that uses a `<canvas>` for scrub/paused frames (from `/render-frame`) and a `<video>` for playback (from `/preview-stream` via MSE). WebGL stays in-tree until task 36.

---

## Context

The backend now ships scrub JPEGs and playback fMP4 fragments. The frontend `BeatEffectPreview` shader does per-layer compositing in the browser and hits the perf ceiling this whole milestone is trying to eliminate. This task swaps the compositor without deleting WebGL yet — keeps `<BeatEffectPreview>` available for rollback during the swap PR, then task 36 deletes it.

---

## Steps

### 1. Backend client (`src/lib/preview-client.ts`)

```ts
export async function fetchScrubFrame(project: string, t: number, quality = 85): Promise<ImageBitmap>
export function openPreviewStream(project: string): PreviewStream
```

- `fetchScrubFrame` — GET `/api/projects/:name/render-frame?t=X&quality=N`, decode response as `ImageBitmap`
- `openPreviewStream` — returns object with `play(t)`, `seek(t)`, `pause()`, `close()` and an `onFragment(cb)` subscriber. Wraps the WebSocket protocol from task 34.

### 2. Latest-wins scrub queue hook (`src/hooks/useLatestWinsRequest.ts`)

```ts
function useLatestWinsRequest<T>(): { request: (key: T, fn: (key: T) => Promise<void>) => void }
```

- Aborts any in-flight request when a new one arrives (via `AbortController`)
- Only resolves the latest; older ones are discarded silently
- Prevents stale-frame flicker on rapid scrubbing

### 3. MSE playback hook (`src/hooks/useMSEPlayback.ts`)

```ts
function useMSEPlayback(
  videoRef: React.RefObject<HTMLVideoElement>,
  projectName: string,
  playing: boolean,
  seekTo: number,
): void
```

- Creates a `MediaSource`, binds to `videoRef.current.src`
- On `sourceopen`: adds a `SourceBuffer` with `video/mp4; codecs="avc1.42E01E"`
- Opens the preview-stream WebSocket, forwards fragments into the buffer
- Handles seek: forwards `{action: 'seek', t}` over WS, clears any queued client-side buffer
- On unmount / `playing=false`: calls `MediaSource.endOfStream()` and closes the WS

### 4. PreviewViewport component (`src/components/editor/PreviewViewport.tsx`)

```tsx
type PreviewViewportProps = {
  projectName: string
  currentTime: number
  playing: boolean
}

type PreviewViewportHandle = {
  getCanvas: () => HTMLCanvasElement | null
  getVideo: () => HTMLVideoElement | null
}

export const PreviewViewport = forwardRef<PreviewViewportHandle, PreviewViewportProps>(...)
```

- Renders a relative container with stacked `<video>` and `<canvas>` children
- Z-index toggled by `playing`: video on top during playback, canvas on top otherwise
- Scrub: `useEffect([playing, currentTime])` → if paused, request `/render-frame`, draw bitmap to canvas
- Playback: `useMSEPlayback(videoRef, projectName, playing, currentTime)`
- Internal "no content" state rendered in canvas when backend returns 404
- Internal scrub-loading spinner visible briefly during cold fetches

### 5. PreviewPanel integration (`src/components/editor/PreviewPanel.tsx`)

Per design doc §PreviewPanel integration:

- Swap `<BeatEffectPreview>` for `<PreviewViewport>`
- Drop the `currentKeyframe?.hasSelectedImage || crossfadeData.frameA` gating check
- Drop props: `src`, `beats`, `audioEvents`, `userEffects`, `suppressions`, `canvasWidth`, `canvasHeight`, `transitionFrameA`, `transitionFrameB`, `blendFactor`, `layers`
- Keep hover overlays (`hoverPreviewUrl`, `hoverVideo`) and `<TransformHandles>` untouched

### 6. PreviewContext shrinkage (`src/components/editor/PreviewContext.tsx`)

Remove from context:
- `crossfadeData`, `trackLayers`, `isTransitionLoading`, `updatePreview`
- `BeatEffectPreviewHandle` type (replace with import from `PreviewViewport`)

Keep:
- `hoverPreviewUrl`, `setHoverPreviewUrl`, `hoverVideo`, `setHoverVideo`, `previewRef`

### 7. Timeline cleanup (`src/components/editor/Timeline.tsx`)

- Delete the `trackLayers` memo (builds `TrackLayer[]` from track data + frame cache)
- Delete the transition-frame preloader that feeds `crossfadeData`
- Delete the `updatePreview(...)` call that pushes data into context
- Keep `currentTime`, `isPlaying` state machinery

### 8. Recording path update (`src/lib/preview-recorder.ts`)

- Current API: `recordPreview({ canvas, audioElement, ... })`
- New API: `recordPreview({ handle: PreviewViewportHandle, audioElement, ... })`
- Inside, pick the right surface based on `isPlaying` (exposed via handle getter or inferred from DOM visibility). During playback: `captureStream()` the `<video>`. While paused: the `<canvas>`.
- `Timeline.tsx:2576` updates accordingly: `const handle = previewRef.current; ...`

---

## Verification

- [ ] `fetchScrubFrame` returns a valid `ImageBitmap` from the endpoint
- [ ] `useLatestWinsRequest` aborts in-flight requests when a new one fires
- [ ] `useMSEPlayback` binds a MediaSource to the video element and feeds fragments from WS
- [ ] `<PreviewViewport>` swaps canvas↔video based on `playing` without flicker
- [ ] Scrubbing drags frames update the canvas smoothly (cached HIT < 20ms)
- [ ] Pressing play transitions to `<video>` playback with no audible/visible glitch
- [ ] Seek during playback emits `{action: 'seek'}` over WS and resumes quickly
- [ ] `<PreviewPanel>` still renders hover overlays and `<TransformHandles>` correctly
- [ ] Timeline's `trackLayers` / preloader / `updatePreview` code is deleted with no orphaned references
- [ ] `preview-recorder.ts` records WebM during both scrub and playback modes
- [ ] `<BeatEffectPreview>` still exists but is no longer referenced anywhere (grep-verifiable)

---

## Expected Output

### Files Created
- `src/lib/preview-client.ts`
- `src/hooks/useLatestWinsRequest.ts`
- `src/hooks/useMSEPlayback.ts`
- `src/components/editor/PreviewViewport.tsx`

### Files Modified
- `src/components/editor/PreviewPanel.tsx` — swap compositor
- `src/components/editor/PreviewContext.tsx` — shrink context
- `src/components/editor/Timeline.tsx` — remove frame-loading pipeline
- `src/lib/preview-recorder.ts` — accept handle instead of raw canvas

---

## Common Issues and Solutions

### Issue 1: Canvas → video flicker on play
**Symptom**: Brief flash of old canvas content when transitioning to playback
**Solution**: Before hiding canvas, draw the current video frame to it (via `requestVideoFrameCallback`). On play, the canvas already shows the same frame, so z-index swap is invisible.

### Issue 2: Scrub requests build up on aggressive drag
**Symptom**: Spinner flashes repeatedly even though the latest frame has rendered
**Solution**: Verify `useLatestWinsRequest` correctly aborts older requests. Log pending request count during scrub to confirm.

### Issue 3: `SourceBuffer.appendBuffer` throws `QuotaExceededError`
**Symptom**: Playback halts after a few minutes
**Solution**: Call `sourceBuffer.remove(0, currentTime - 10)` periodically to trim the playback buffer behind the playhead.

---

**Status**: Not Started
