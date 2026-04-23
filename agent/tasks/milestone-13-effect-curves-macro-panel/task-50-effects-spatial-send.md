# Task 50: Spatial + send effect implementations

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R7-R9, R12-R13
**Estimated Time**: 4 hours
**Dependencies**: T46 (registry), T47 (graph + bus infra)
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Implement the 5 effect-type `build()` factories for spatial + send effects: `pan`, `stereo_width`, `reverb_send`, `delay_send`, `echo_send`.

---

## Steps

### 1. Pan

`src/lib/audio-effects/pan.ts`:
- `StereoPannerNode` directly
- `input` = `output` = the panner
- Animatable: `pan` (range -1 to +1, scale linear)

### 2. Stereo width

`src/lib/audio-effects/stereo-width.ts`:
- Mid/side processing via `ChannelSplitterNode` + `GainNode`s + `ChannelMergerNode`
- `width` param: 0 = mono (sum L+R), 0.5 = normal stereo, 1 = extra-wide (inverted center)
- Animatable: `width` (range 0 to 2, scale linear)

### 3. Reverb / Delay / Echo sends

Sends are NOT effects on the track chain. They tap the track's output and route a portion to a project-scoped bus (built in T47). Treat each as a thin wrapper:

`src/lib/audio-effects/reverb-send.ts` (and similar for `delay-send.ts`, `echo-send.ts`):

```ts
export function buildReverbSend(ctx: AudioContext, staticParams: { bus_id: string }): EffectNode {
  // Lookup the bus from audio-mixer's bus registry
  const bus = getBus(staticParams.bus_id)
  if (!bus) throw new Error(`reverb_send: bus ${staticParams.bus_id} not found`)

  // Send effects don't interrupt the main chain. Their 'input' and 'output' are
  // the same GainNode that passes signal through unchanged. The MIXER wires a
  // parallel tap from the track's track_gain to the bus, with send level
  // animating the tap gain.
  const passthrough = ctx.createGain()
  passthrough.gain.value = 1  // unity; not animated

  // Register a send-level GainNode that IS animated, via the mixer's send-routing
  const sendGain = ctx.createGain()
  sendGain.gain.value = 0  // default; animatable via this effect's 'wet' curve

  return {
    input: passthrough,
    output: passthrough,
    setParam: (name, value, when) => {
      if (name === 'wet') sendGain.gain.setValueAtTime(value, when ?? ctx.currentTime)
      else throw new Error(`reverb_send: unknown param ${name}`)
    },
    scheduleCurve: (name, points, startTime, duration) => {
      if (name === 'wet') scheduleCurveOnParam(sendGain.gain, points, 'linear', 'linear', { min: 0, max: 1 }, startTime, duration)
      else throw new Error(`reverb_send: unknown param ${name}`)
    },
    dispose: () => {
      passthrough.disconnect()
      sendGain.disconnect()
      // Note: the mixer's send-routing logic tracks sendGain externally and
      // cleans up the track→bus wiring when the effect is disposed.
    },
  }
}
```

`delay_send` and `echo_send` are identical in shape; only differ in `bus_type` and which bus they route to.

### 4. Mixer integration

Update the audio-mixer's chain-builder (T47) to detect send-type effects and wire their `sendGain` to the corresponding bus input when building the chain.

Add a registry function `getBus(bus_id)` exposing the mixer's bus map.

### 5. Tests

`src/lib/audio-effects/__tests__/pan.test.ts`, `stereo-width.test.ts`, `*-send.test.ts`:
- Pan at -1 routes to left channel only
- Pan at +1 routes to right channel only
- Stereo width 0 produces mono
- Stereo width 1 is identity
- Send-level curve animates audible bus output
- Adding a reverb_send with `bus_id` pointing to a nonexistent bus throws on build

---

## Verification

- [ ] Pan knob animates L-R placement audibly
- [ ] Stereo width produces correct mid/side behavior at 0, 0.5, 1.0
- [ ] Reverb send with IR-loaded bus produces audible reverb tail
- [ ] Send level curve sweeps reverb wet from 0 to max
- [ ] Delay send produces rhythmic feedback taps
- [ ] Echo send produces single-tap analog-style repeat
- [ ] Tests pass

---

## Notes

- The send-effect pattern is unusual: it's passthrough on the main chain, but writes to a parallel tap. That's why `input === output` on the main chain, and the real audio routing happens externally in the mixer's bus-routing logic.
- Stereo width implementation: use [classic mid/side math](https://en.wikipedia.org/wiki/Joint_(audio_engineering)#Mid/side_joint). Mid = (L+R)/2, Side = (L-R)/2. Width applied to Side. Recombine.
- If stereo-width is too fiddly to implement cleanly in WebAudio, defer to an AudioWorklet in a follow-up — but try WebAudio built-ins first.
