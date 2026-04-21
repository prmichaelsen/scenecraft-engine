# Task 36: WebGL removal + end-to-end validation

**Milestone**: [M11 - Backend-Rendered Preview Streaming](../../milestones/milestone-11-backend-rendered-preview-streaming.md)
**Design Reference**: [backend-rendered-preview-streaming](../../design/local.backend-rendered-preview-streaming.md) (see §WebGL removal)
**Estimated Time**: 1-2 days
**Dependencies**: Task 35 (`<PreviewViewport>` fully swapped in)
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Delete the WebGL compositor and its support code now that `<PreviewViewport>` handles all preview rendering. No feature flag, no rollback path — `git revert` is the safety net.

---

## Context

This is intentionally the last task in the milestone. Until task 35 ships and is stable in production use, `<BeatEffectPreview>` stays on disk as dead code so we can roll back by flipping the import. Once we're confident, delete it.

---

## Steps

### 1. Pre-deletion audit

- Run `grep -rn "BeatEffectPreview\|WebGLRenderingContext\|gl.createShader\|frame-cache" src/`
- Verify no active import sites remain. The only hits should be inside the files targeted for deletion.

### 2. Delete frontend files

- `src/components/editor/BeatEffectPreview.tsx` — WebGL shader + per-layer compositing
- `src/lib/frame-cache.ts` — frontend frame preloader (no longer needed; backend has its own L1 cache)
- Any shader constants / framebuffer utilities these pull in (audit imports first)

### 3. Clean up dead dependencies

- `src/components/editor/Timeline.tsx` — remove `preloadTransition`, `preloadKeyframeImage`, `setPlayheadPosition`, `setEvictionProtectWindow`, etc. imports from the deleted `frame-cache.ts`
- Any other files that imported `TrackLayer` from `BeatEffectPreview` — update to use the new type location or delete

### 4. Verify no regressions

- Build: `npm run build` succeeds with no errors
- Type check: `npm run typecheck` clean (or at least no NEW errors introduced)
- Smoke-test the editor:
  - Open a project, scrub the timeline → canvas updates
  - Press play → switches to `<video>` playback
  - Edit a transition mid-playback → stutters briefly, resumes with edit applied
  - Hover a candidate → overlay renders correctly
  - Select a transition → `<TransformHandles>` appear
  - Record a preview → WebM downloads successfully
- Compare scrubbed frames against a full backend render of the same time range — should be pixel-identical

### 5. Documentation

- Update `CHANGELOG.md` with the WebGL removal
- Update any READMEs or design docs that reference `BeatEffectPreview`
- Close out M11 in `progress.yaml`: set `status: completed`, `completed: <date>`

---

## Verification

- [ ] `src/components/editor/BeatEffectPreview.tsx` deleted
- [ ] `src/lib/frame-cache.ts` deleted
- [ ] No remaining imports of deleted files anywhere in `src/`
- [ ] `grep -rn "WebGL\|gl.createShader" src/` returns no hits in live code
- [ ] Build succeeds with no new errors
- [ ] Type check clean (or no new errors)
- [ ] Manual smoke-test checklist passed (all 6 items above)
- [ ] Scrub frames match full-render frames pixel-for-pixel
- [ ] CHANGELOG updated
- [ ] `progress.yaml` M11 marked completed

---

## Expected Output

### Files Deleted
- `src/components/editor/BeatEffectPreview.tsx`
- `src/lib/frame-cache.ts`
- Any WebGL-specific helpers referenced only by the above

### Files Modified
- `src/components/editor/Timeline.tsx` — drop dead imports
- `CHANGELOG.md`
- `agent/progress.yaml`

---

## Common Issues and Solutions

### Issue 1: TypeScript errors about missing `TrackLayer` type
**Symptom**: `Cannot find name 'TrackLayer'` in some file
**Solution**: `TrackLayer` was defined in `BeatEffectPreview.tsx` and re-exported. After deletion, find the remaining importer and either delete the reference (it should no longer be meaningful) or move the type to a shared location. Most references should already be gone after task 35.

### Issue 2: Hover preview overlays broken
**Symptom**: Hovering a candidate doesn't show the preview
**Solution**: Verify `PreviewPanel.tsx` still renders `hoverPreviewUrl` / `hoverVideo` branches. These are independent of WebGL.

### Issue 3: Recording produces empty video
**Symptom**: `recordPreview` output plays as blank
**Solution**: Confirm `preview-recorder.ts` picks the right stream source. During scrub it should `captureStream` the canvas; during playback the video. Verify via console logging inside the recorder.

---

**Status**: Not Started
