"""Keyframe and transition candidate generation — minimal helpers used by the chat tools.

These functions kick off the same underlying Imagen / Veo pipelines that
api_server.py exposes over HTTP, but without the HTTP layer and with a smaller
option surface suited to an AI assistant call.

Intentional duplication with api_server's _handle_generate_{keyframe,transition}_candidates
handlers — those handlers accept a richer option set (freeform, refinement, ingredients,
seeds, useNextTransitionFrame, noEndFrame, etc.). The chat path deliberately uses the
transition/keyframe record's own values for those options so Claude doesn't have to
thread them through every call. If you extend one path, consider extending both.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [chat.gen] {msg}", file=sys.stderr, flush=True)


def _next_variant_num(directory: Path, ext: str = ".png") -> int:
    max_v = 0
    for f in directory.glob(f"v*{ext}"):
        m = re.match(r"v(\d+)", f.stem)
        if m:
            max_v = max(max_v, int(m.group(1)))
    return max_v


def _image_backend(project_dir: Path) -> str:
    from scenecraft.db import get_meta
    return (get_meta(project_dir) or {}).get("image_backend", "vertex")


def start_keyframe_generation(
    project_dir: Path,
    project_name: str,
    kf_id: str,
    count: int,
    prompt_override: str | None = None,
) -> dict:
    """Kick off Imagen generation for a keyframe. Returns {job_id, keyframe_id} or {error}."""
    from scenecraft.db import get_keyframe
    from scenecraft.ws_server import job_manager

    count = max(1, min(int(count), 8))

    kf = get_keyframe(project_dir, kf_id)
    if not kf:
        return {"error": f"keyframe not found: {kf_id}"}

    # Locate source image
    source_rel = kf.get("source") or f"selected_keyframes/{kf_id}.png"
    source_path = project_dir / source_rel
    if not source_path.exists():
        source_path = project_dir / "selected_keyframes" / f"{kf_id}.png"
    if not source_path.exists():
        return {"error": f"no source image for {kf_id}; select an image first"}

    prompt = (prompt_override or kf.get("prompt") or "").strip()
    if not prompt:
        return {"error": f"keyframe {kf_id} has no prompt; pass prompt_override or set keyframe.prompt first"}

    candidates_dir = project_dir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    existing_count = _next_variant_num(candidates_dir, ".png")

    job_id = job_manager.create_job(
        "chat_keyframe_candidates",
        total=count,
        meta={"keyframeId": kf_id, "project": project_name, "source": "chat"},
    )

    img_backend = _image_backend(project_dir)

    def _run():
        try:
            from concurrent.futures import ThreadPoolExecutor
            from scenecraft.render.google_video import GoogleVideoClient
            from scenecraft.db import get_meta, update_keyframe

            client = GoogleVideoClient(vertex=True)
            img_model = (get_meta(project_dir) or {}).get("image_model", "replicate/nano-banana-2")
            completed = {"n": 0}

            def _gen_one(v: int):
                out_path = str(candidates_dir / f"v{v}.png")
                if Path(out_path).exists():
                    completed["n"] += 1
                    job_manager.update_progress(job_id, completed["n"], f"v{v} cached")
                    return
                varied = f"{prompt}, variation {v}" if v > 1 else prompt
                tries = 0
                while tries < 3:
                    try:
                        client.stylize_image(str(source_path), varied, out_path, image_model=img_model)
                        break
                    except Exception as e:
                        tries += 1
                        _log(f"  {kf_id} v{v} attempt {tries} failed: {e}")
                        if tries >= 3:
                            raise
                        time.sleep(5 * tries)
                completed["n"] += 1
                job_manager.update_progress(job_id, completed["n"], f"v{v}")

            variants = list(range(existing_count + 1, existing_count + count + 1))
            with ThreadPoolExecutor(max_workers=count) as pool:
                list(pool.map(_gen_one, variants))

            all_cands = sorted(
                [f"keyframe_candidates/candidates/section_{kf_id}/{f.name}"
                 for f in candidates_dir.glob("v*.png")],
                key=lambda p: int(re.search(r"v(\d+)", p).group(1)),
            )
            update_keyframe(project_dir, kf_id, candidates=all_cands)
            job_manager.complete_job(job_id, {
                "keyframeId": kf_id,
                "candidates": all_cands,
                "added_count": count,
                "total_candidates": len(all_cands),
            })
        except Exception as e:
            _log(f"keyframe gen failed: {e}")
            job_manager.fail_job(job_id, str(e))

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "keyframe_id": kf_id, "count": count, "backend": img_backend}


def start_transition_generation(
    project_dir: Path,
    project_name: str,
    tr_id: str,
    count: int,
    slot_index: int | None = None,
) -> dict:
    """Kick off Veo generation for a transition. Returns {job_id, transition_id} or {error}.

    Inherits ingredients, seed, negativePrompt, and action prompt from the
    transition record — chat doesn't override them for simplicity. If Claude
    needs to tweak those, it should use `update_transition` first, then call
    this tool.
    """
    from scenecraft.db import get_transition, get_meta
    from scenecraft.ws_server import job_manager

    count = max(1, min(int(count), 4))

    tr = get_transition(project_dir, tr_id)
    if not tr:
        return {"error": f"transition not found: {tr_id}"}

    meta = get_meta(project_dir) or {}
    motion_prompt = meta.get("motionPrompt") or meta.get("motion_prompt") or ""
    max_seconds = meta.get("transition_max_seconds") or 8

    from_kf_id = tr.get("from")
    to_kf_id = tr.get("to")
    n_slots = int(tr.get("slots", 1))
    tr_duration = float(tr.get("duration_seconds") or 0)
    action = tr.get("action") or "Smooth cinematic transition"

    if slot_index is not None and (slot_index < 0 or slot_index >= n_slots):
        return {"error": f"slot_index {slot_index} out of range (transition has {n_slots} slots)"}

    selected_kf_dir = project_dir / "selected_keyframes"
    start_img = selected_kf_dir / f"{from_kf_id}.png"
    end_img = selected_kf_dir / f"{to_kf_id}.png"
    if not start_img.exists():
        return {"error": f"start keyframe image not found: {from_kf_id} (select an image for it first)"}
    if not end_img.exists():
        return {"error": f"end keyframe image not found: {to_kf_id}"}

    use_global = bool(tr.get("use_global_prompt", True))
    prompt = f"{action}. Camera and motion style: {motion_prompt}" if use_global and motion_prompt else action
    slot_duration = min(max_seconds, tr_duration / n_slots) if tr_duration > 0 else max_seconds

    ingredients = tr.get("ingredients") or []
    ingredient_paths: list[str] | None = None
    if ingredients:
        resolved = [str(project_dir / p) for p in ingredients if p]
        resolved = [p for p in resolved if Path(p).exists()]
        ingredient_paths = resolved or None

    negative_prompt = tr.get("negativePrompt") or None
    veo_seed = tr.get("seed")

    vid_backend = meta.get("video_backend", "vertex")
    slots_to_process = [slot_index] if slot_index is not None else list(range(n_slots))
    total_jobs = count * len(slots_to_process)

    job_id = job_manager.create_job(
        "chat_transition_candidates",
        total=total_jobs,
        meta={"transitionId": tr_id, "project": project_name, "source": "chat"},
    )

    def _run():
        try:
            from concurrent.futures import ThreadPoolExecutor
            from scenecraft.render.google_video import GoogleVideoClient
            from scenecraft.db import add_tr_candidate
            import uuid as _uuid

            if vid_backend.startswith("runway"):
                from scenecraft.render.google_video import RunwayVideoClient
                _, _, runway_model = vid_backend.partition("/")
                client = RunwayVideoClient(model=runway_model or "veo3.1_fast")
            else:
                client = GoogleVideoClient(vertex=True)

            pool_segs_dir = project_dir / "pool" / "segments"
            pool_segs_dir.mkdir(parents=True, exist_ok=True)

            completed = {"n": 0}
            generated_segments: list[dict] = []

            def _gen_one(si: int, i: int):
                # Pre-generate the pool_segment UUID so the file can land at its final
                # path in one shot — avoids a post-rename UPDATE that races with the
                # undo triggers and sometimes hits "database is locked".
                seg_id = _uuid.uuid4().hex
                final_filename = f"{seg_id}.mp4"
                out_path = pool_segs_dir / final_filename

                s_img = str(start_img) if si == 0 else str(project_dir / "selected_slot_keyframes" / f"{tr_id}_slot_{si - 1}.png")
                e_img = str(end_img) if si == n_slots - 1 else str(project_dir / "selected_slot_keyframes" / f"{tr_id}_slot_{si}.png")
                if not Path(s_img).exists():
                    s_img = str(start_img)
                if not Path(e_img).exists():
                    e_img = str(end_img)

                tries = 0
                while tries < 3:
                    try:
                        client.generate_video(
                            s_img,
                            e_img,
                            prompt,
                            str(out_path),
                            duration_seconds=slot_duration,
                            ingredient_paths=ingredient_paths,
                            negative_prompt=negative_prompt,
                            seed=veo_seed,
                        )
                        break
                    except Exception as e:
                        tries += 1
                        _log(f"  {tr_id} slot{si} v{i} attempt {tries} failed: {e}")
                        if tries >= 3:
                            raise
                        time.sleep(10 * tries)

                # Register with the pool — single INSERT, retryable on lock.
                from scenecraft.db import get_db, _retry_on_locked
                now_iso = datetime.now().astimezone().isoformat()

                def _insert_pool_seg():
                    conn = get_db(project_dir)
                    conn.execute(
                        """INSERT INTO pool_segments
                           (id, pool_path, kind, created_by, duration_seconds, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (seg_id, f"pool/segments/{final_filename}", "generated",
                         "chat_generation", slot_duration, now_iso),
                    )
                    conn.commit()
                _retry_on_locked(_insert_pool_seg)

                add_tr_candidate(
                    project_dir,
                    transition_id=tr_id,
                    slot=si,
                    pool_segment_id=seg_id,
                    source="generated",
                )

                generated_segments.append({
                    "pool_segment_id": seg_id,
                    "transition_id": tr_id,
                    "slot": si,
                    "path": f"pool/segments/{final_filename}",
                })
                completed["n"] += 1
                job_manager.update_progress(job_id, completed["n"], f"slot{si} v{i+1}")

            jobs = []
            for si in slots_to_process:
                for i in range(count):
                    jobs.append((si, i))

            with ThreadPoolExecutor(max_workers=min(count, 4)) as pool:
                list(pool.map(lambda args: _gen_one(*args), jobs))

            job_manager.complete_job(job_id, {
                "transitionId": tr_id,
                "generated": generated_segments,
                "added_count": len(generated_segments),
            })
        except Exception as e:
            _log(f"transition gen failed: {e}")
            job_manager.fail_job(job_id, str(e))

    threading.Thread(target=_run, daemon=True).start()
    return {
        "job_id": job_id,
        "transition_id": tr_id,
        "count": count,
        "slots": slots_to_process,
        "backend": vid_backend,
    }
