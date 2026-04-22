# Task 43: Timeline render-state UI bar

**Milestone**: [M12 - NLE-Style Preview Rendering Pipeline](../../milestones/milestone-12-nle-preview-pipeline.md)
**Design Reference**: None (design captured in the milestone doc)
**Estimated Time**: 1-2 days
**Dependencies**: Task 42 (render-state tracking API + WS push), Task 37 (unified WS) for live updates
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Add a thin colored strip above the Timeline's playhead ruler that shows, per time-bucket, the current render state. Mirrors Premiere's "render bar" / Resolve's render cache indicator — the user can see at a glance which regions will play instantly vs which still need to be rendered.

---

## Context

Pro NLEs always tell you what's rendered. It's the single most important UX affordance for preview performance — users stop wondering "why is playback stuttering here" because they can see "oh, this region is dark-red, it's not rendered yet."

---

## Steps

### 1. `<RenderStateBar>` component

`src/components/editor/RenderStateBar.tsx`:

Props:
```ts
type Props = {
  projectName: string
  pxPerSec: number
  duration: number   // total timeline duration, seconds
  scrollLeft: number // synced with Timeline's horizontal scroll
  height?: number    // default 4
}
```

Renders a fixed-height horizontal strip spanning the full duration at `pxPerSec` scale. Inside: a series of `<div>` spans, one per bucket, colored per state.

### 2. Color tokens

- **Unrendered**: dark red — `#7f1d1d` (Tailwind `red-900`)
- **Rendering**: bright red — `#ef4444` (Tailwind `red-500`)
- **Cached**: blue — `#3b82f6` (Tailwind `blue-500`)
- **Stale**: dark red with 45° diagonal stripes via CSS `repeating-linear-gradient` — distinct from plain dark red

Stale stripe pattern example:
```css
background: repeating-linear-gradient(
  45deg,
  #7f1d1d 0 6px,
  #991b1b 6px 12px
);
```

### 3. State source: `useRenderState(projectName)` hook

`src/hooks/useRenderState.ts`:

```ts
export function useRenderState(projectName: string): {
  buckets: Array<{ t_start: number; t_end: number; state: BucketState }>
  loading: boolean
  error: string | null
} { ... }
```

On mount:
1. Fetches `GET /api/projects/:name/render-state` for initial snapshot
2. Subscribes to the unified WS (task-37) for `render-state.update` deltas
3. Applies deltas to local state
4. Unsubscribes on unmount

Returns a stable reference when only the deltas change (uses a Map under the hood; snapshots convert to array for render).

### 4. Render optimization

Timeline can be long (hours). At 2s buckets that's thousands of spans. Mount strategy:

- **Virtualize**: only render spans visible in the current scroll window (`scrollLeft` to `scrollLeft + visibleWidth`)
- Pre-compute visible bucket range on scroll/zoom change
- Memoize span elements — the DOM is cheap for a few hundred visible spans

### 5. Placement in Timeline

Thin strip directly above the existing playhead ruler, full width of the timeline scroll container. Shares the same `scrollLeft` / `pxPerSec`.

Not clickable (read-only). Hover to show tooltip with bucket timestamp + state.

### 6. Tooltip (optional, phase-2 polish)

On hover over a bucket:
- `cached 3.2s ago` (shows how stale-free it is)
- `rendering…` (plus estimated time if background worker exposes it)
- `unrendered`
- `stale — edited 12s ago` (if task-42 exposes the invalidation timestamp)

### 7. Tests

- Component test: given a bucket list, renders correct colors in the right positions
- Hook test: snapshot fetch + delta application produces consistent state

### 8. Feature flag

Gate behind a per-user localStorage setting (e.g., `scenecraft.preview.showRenderBar`, default on). Easy to disable if the UI is too noisy during dev.

---

## Verification

- [ ] On project open: render-state bar populates within 1s with initial snapshot
- [ ] Hitting play: buckets near the playhead transition dark-red → bright-red → blue in real time as background + playback renders produce them
- [ ] Editing a keyframe: affected buckets immediately flip to dark-red-striped stale
- [ ] Scrolling the Timeline: only visible buckets are mounted (verified via devtools — span count matches visible range, not total duration)
- [ ] Bar width/position stays aligned with Timeline playhead ruler at all zoom levels
- [ ] Hover tooltip (if shipped) shows correct state + timestamp
- [ ] Unmounting the Timeline unsubscribes from WS updates cleanly (no leaks)

---

## Key Design Decisions

### Model

| Decision | Choice | Rationale |
|---|---|---|
| Bar position | Above playhead ruler | Standard NLE placement; user's eye is already there when judging playback |
| Virtualization | Yes | 2.4h @ 2s buckets = 4340 spans — noticeable perf hit without virtualization |
| Click interaction | None | Read-only status indicator; click-to-render is future |
| Live updates | WS push (via task-37) | Polling would either lag UX or burn bandwidth |
| Feature flag | localStorage per-user | Easy toggle for debugging without pulling the feature |

---

## Notes

- Color choices are tailwind-friendly and match the user's stated preference: dark red (unrendered) + bright red (rendering) + blue (cached) + dark-red-striped (stale).
- Future: click a stale bucket to force re-render. Ctrl-click to clear a cached bucket (force re-render on next play). Not in scope.
- Future: show storage cost overlay — "fragment cache 340MB / 500MB" somewhere in the bar area. Not in scope.
