# isolate_vocals plugin

First-party scenecraft plugin that separates a voice-over-noise audio source
into `vocal` + `background` stems using DeepFilterNet3 + a time-domain residual.

## Installation

DeepFilterNet3 is an optional dependency:

```
pip install scenecraft-engine[plugins]
```

On first use the DFN3 Python package downloads its pretrained weights (~30 MB)
to its cache directory. No GPU required; CPU inference runs roughly at
realtime on modern laptops.

## Invocation

Three surfaces, all landing on the same `run()` handler:

- **AudioIsolationsPanel** (frontend) — primary UX. Select an audio clip,
  click Run, watch progress in the panel.
- **REST**: `POST /api/projects/:name/plugins/isolate_vocals/run` with
  `{entity_type, entity_id, range_mode, trim_in?, trim_out?}`.
- **Chat tool** `isolate_vocals` — requires user confirmation via the
  destructive-tool elicitation flow.

All three produce one `audio_isolations` row grouping the `vocal` + `background`
stems via `isolation_stems`. Each stem is a fresh `pool_segments` row with
`kind='generated'`.

## Scope (MVP)

- `audio_clip` entities only. `transition` source extraction is a follow-up.
- 2 stems per run: `vocal` (DFN3 output) + `background` (`source − vocal`).
- Full-source or subset range (`trim_in`/`trim_out` seconds).

## Failure modes

- Missing dependency → `ImportError` → `fail_job` with "install deepfilternet"
  hint in the stderr log.
- Source file missing on disk → synchronous `{error}` at kickoff.
- DFN3 runtime error → `fail_job` + `audio_isolations.status='failed'` with
  the error string persisted.
