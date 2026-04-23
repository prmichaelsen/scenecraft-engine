# Task 47: Audio graph runtime + send buses + IR assets

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R11-R15, R53-R54
**Estimated Time**: 6 hours
**Dependencies**: T45 (schema), T46 (registry)
**Status**: Not Started
**Repository**: `scenecraft` (frontend) + `scenecraft-engine` (IR asset shipping)

---

## Objective

Build the runtime WebAudio graph for per-track effect chains + project-scoped send buses. Ship 6 bundled impulse-response WAV files for ConvolverNode reverb.

---

## Steps

### 1. Audio graph topology per track

Extend `src/lib/audio-mixer.ts`:

```
audio_source → volume_gain (existing) → effect_1.input → ... → effect_N.output → pan → track_gain → destination
                                                                                              ↓ (parallel tap)
                                               for each bus: track_gain → sendGain(bus) → bus.input
```

### 2. Graph rebuild trigger

When `track_effects` rows change (add/remove/reorder/enable toggle):
1. Dispose old chain nodes (call `effect.dispose()` on each)
2. Build new chain from `list_track_effects(project, track_id)` in `order_index` order
3. Effects with `enabled=0` are built but their `.input` is wired to the same downstream as their `.output` would be (bypass; internal state preserved)
4. Reconnect source → chain → pan → gain → destination + per-bus sends
5. Emit `mixer.chain-rebuilt {track_id}` event

### 3. Send bus graph

Per project, build 4 default buses (2 reverb + 1 delay + 1 echo) on mixer init:
- Reverb buses: `ConvolverNode` with IR loaded from `{project}/proxies/../impulse_responses/{ir_name}.wav` OR bundled asset path
- Delay bus: `DelayNode` + feedback loop (`GainNode` looped back to input)
- Echo bus: `DelayNode` single-tap + tone `BiquadFilter` (lowpass ~4kHz default)

Each bus exposes `input: AudioNode, output: AudioNode, setParam, dispose` (same `EffectNode` shape). Bus output → destination.

### 4. Per-track send routing

For each `(track_id, bus_id)` in `track_sends`:
- A `GainNode` on the path `track_gain → sendGain → bus.input`
- `sendGain.gain.value = level` (from DB)
- This GainNode is the animation target for send-level curves

### 5. IR asset shipping

Create `src/scenecraft/assets/impulse_responses/` in the backend. Populate with 6 IR files:
- `room-small.wav` (~20KB)
- `room-large.wav` (~35KB)
- `hall.wav` (~50KB)
- `plate.wav` (~30KB)
- `spring.wav` (~25KB)
- `chamber.wav` (~40KB)

Source from a CC-licensed IR library (e.g. OpenAIR) or record simple synthetic IRs. Total gzipped ≤ 200KB per spec R53.

Serve from a backend HTTP endpoint `GET /api/assets/impulse_responses/:name` with appropriate Cache-Control headers (immutable content, max-age=31536000).

### 6. IR loading on client

In the audio-mixer, when a reverb bus's `static_params.ir` is set to a built-in IR name:
- Fetch via `/api/assets/impulse_responses/:name`
- Decode via `audioCtx.decodeAudioData`
- Assign to `convolverNode.buffer`

When `static_params.ir` is a pool file path:
- Fetch via existing `scenecraftFileUrl(projectName, path)`
- Same decode path

### 7. Missing-IR fallback

If the IR file is missing (404 or decode error):
- Log warning with bus id + IR name
- Leave `convolverNode.buffer = null` (audio passes through with no convolution)
- Emit event for the UI to surface a banner (actual UI banner is in T54)

### 8. Disable / enable in place

`effect.dispose()` should NOT be called on enable/disable toggle. Instead:
- Keep `effect.input` and `effect.output` nodes alive
- On disable: disconnect `effect.input → effect.inner...` and connect `effect.input → effect.output` directly (bypass)
- On enable: reconnect inner chain

### 9. Tests

`src/lib/__tests__/audio-mixer-effects.test.ts`:
- Adding an effect rebuilds the chain and the new node is at the correct position
- Disabling an effect bypasses it without destroying curve state
- Reordering effects reflects in chain topology
- Send bus receives signal from track gain through its `sendGain` node
- Missing IR logs a warning and leaves bus passthrough

Unit tests mock WebAudio via the existing `audio-mixer.test.ts` fixtures.

---

## Verification

- [ ] Track with 3 effects builds a chain with exactly 3 effect nodes between volume_gain and pan
- [ ] Disabling effect bypasses audibly but preserves inner state
- [ ] Re-enabling restores effect without rebuilding neighbors
- [ ] Reordering POST updates chain topology within one frame
- [ ] All 4 default buses wired from mixer init
- [ ] Per-track send gain animatable via AudioParam
- [ ] IR assets decode correctly; ConvolverNode produces audible reverb tail
- [ ] Missing IR falls back cleanly with warning logged
- [ ] Tests pass

---

## Notes

- IR asset sourcing: record in a real space with a starter pistol or impulse generator, OR synthesize via `audioCtx.createBuffer` with Gaussian-decay white noise. Either works for bundled v1 IRs.
- Bundle IRs via the backend HTTP endpoint, NOT via webpack bundle — keeps frontend bundle size small and allows the same IRs for future audio processing tasks.
- No ffmpeg involvement here; IRs are short WAV files.
- Chain dispose order matters: tear down from output to input to avoid disconnecting active audio.
