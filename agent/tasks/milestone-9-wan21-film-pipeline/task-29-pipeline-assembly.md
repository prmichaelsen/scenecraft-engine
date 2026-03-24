# Task 29: Wan Pipeline Assembly

**Milestone**: [M9 - Wan2.1 + FILM Pipeline](../../milestones/milestone-9-wan21-film-pipeline.md)
**Design Reference**: [Wan2.1 + FILM Pipeline](../../design/local.wan21-film-pipeline.md)
**Estimated Time**: 4 hours
**Dependencies**: Task 26 (Wan2.1 Workflow), Task 27 (FILM Integration), Task 28 (Engine Selection)
**Status**: Not Started

---

## Objective

Wire up the full end-to-end Wan2.1 + FILM pipeline: section chunking → Wan2.1 rendering → intra-section FILM smoothing → inter-section FILM transitions → video reassembly. Implement per-clip caching, download-as-generated, and work directory integration.

---

## Steps

### 1. Wan Pipeline Orchestrator
- Create `src/beatlab/render/wan_pipeline.py`
- `render_wan(video_file, beat_map, effect_plan, work_dir, ...) -> output_path`
- Orchestrates: chunk → render → FILM → reassemble

### 2. Per-Clip Caching & Live Download
- Each Wan2.1 clip saved to `wan_clips/section_NNN_chunk_NNN.mp4`
- On resume, skip clips that already exist in work dir
- Download clips from remote GPU as each completes (not batch at end)
- Print progress: `  Rendered section 3/30, clip 2/4 — downloaded`

### 3. FILM Stitching Pass
- After all Wan2.1 clips are rendered:
  - Run intra-section FILM between consecutive clips in each section (~4-8 frames)
  - Run inter-section FILM at section boundaries (AI-controlled length)
- Cache transition clips to `transitions/` dir

### 4. Final Reassembly
- Concatenate: section clips (with intra-FILM smoothed) + inter-section transitions
- Mux original audio back in
- Output final video

### 5. Integration with `beatlab render`
- Wire into CLI: when `--engine wan`, call wan_pipeline instead of ebsynth pipeline
- Pass `--preview` resolution through
- Generate Fusion .setting alongside (existing code, unchanged)

---

## Verification

- [ ] Full pipeline runs end-to-end: video in → stylized video out
- [ ] Per-clip caching works — interrupt and resume produces same result
- [ ] Clips download as generated during remote render
- [ ] Intra-section FILM smooths clip boundaries
- [ ] Inter-section FILM creates style morph transitions
- [ ] Audio is preserved in final output
- [ ] `--preview` produces 512x512 output
- [ ] Fusion .setting still generated alongside
