"""Candidates router — unselected + video candidates + staging
(M16 T63).

Mirrors legacy ``_handle_promote_staged_candidate`` /
``_handle_generate_staged_candidate`` plus the GET-only listing routes.
``GET /staging/{stagingId}`` returns a filesystem scan of
``staging/{stagingId}/v*.png`` — legacy returns
``{"candidates": []}`` for a missing dir, which we preserve.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, Request, status

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError
from scenecraft.api.models.media import GenerateStagedBody, PromoteStagedBody

router = APIRouter(tags=["candidates"], dependencies=[Depends(current_user)])


# ---------------------------------------------------------------------------
# GET /unselected-candidates
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/unselected-candidates",
    operation_id="list_unselected_candidates",
    summary="List keyframe candidate images NOT currently selected.",
)
async def list_unselected_candidates(
    name: str, pdir: Path = Depends(project_dir)
) -> dict:
    from scenecraft.db import get_keyframes

    kfs = get_keyframes(pdir)
    candidates: list[dict] = []
    seen_hashes: set[str] = set()
    for kf in kfs:
        kf_id = kf["id"]
        selected = kf.get("selected")
        cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
        if not cand_dir.is_dir():
            continue
        for f in sorted(cand_dir.glob("v*.png"), key=lambda p: int(p.stem.replace("v", ""))):
            vnum = int(f.stem.replace("v", ""))
            if vnum == selected:
                continue
            with open(f, "rb") as fh:
                file_hash = hashlib.md5(fh.read(8192)).hexdigest()
            if file_hash in seen_hashes:
                continue
            seen_hashes.add(file_hash)
            candidates.append(
                {
                    "keyframeId": kf_id,
                    "variant": vnum,
                    "path": f"keyframe_candidates/candidates/section_{kf_id}/{f.name}",
                }
            )
    return {"candidates": candidates}


# ---------------------------------------------------------------------------
# GET /video-candidates
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/video-candidates",
    operation_id="list_video_candidates",
    summary="List every generated video candidate (pool-joined, by recency).",
)
async def list_video_candidates(
    name: str, limit: int = 100, pdir: Path = Depends(project_dir)
) -> dict:
    if not (pdir / "project.db").exists():
        return {"candidates": []}

    from scenecraft.db import get_db as _get_db

    conn = _get_db(pdir)
    rows = conn.execute(
        """SELECT tc.transition_id, tc.slot, tc.added_at,
                  ps.id, ps.pool_path, ps.byte_size, ps.duration_seconds
           FROM tr_candidates tc
           JOIN pool_segments ps ON ps.id = tc.pool_segment_id
           ORDER BY tc.added_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return {
        "candidates": [
            {
                "transitionId": row["transition_id"],
                "slot": f"slot_{row['slot']}",
                "poolSegmentId": row["id"],
                "path": row["pool_path"],
                "size": row["byte_size"],
                "durationSeconds": row["duration_seconds"],
                "addedAt": row["added_at"],
            }
            for row in rows
        ]
    }


# ---------------------------------------------------------------------------
# GET /staging/{stagingId}
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/staging/{stagingId}",
    operation_id="get_staging",
    summary="List staged candidate images for a staging id.",
)
async def get_staging(
    name: str, stagingId: str, pdir: Path = Depends(project_dir)
) -> dict:
    staging_dir = pdir / "staging" / stagingId
    if not staging_dir.is_dir():
        return {"candidates": []}
    candidates = sorted(
        [f"staging/{stagingId}/{f.name}" for f in staging_dir.glob("v*.png")],
        key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]),
    )
    return {"candidates": candidates}


# ---------------------------------------------------------------------------
# POST /promote-staged-candidate
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/promote-staged-candidate",
    operation_id="promote_staged_candidate",
    summary="Copy a staged candidate image onto a keyframe's selected slot.",
)
async def promote_staged_candidate(
    name: str, body: PromoteStagedBody, pdir: Path = Depends(project_dir)
) -> dict:
    if not body.keyframeId or not body.stagingId:
        raise ApiError(
            "BAD_REQUEST", "Missing 'keyframeId' or 'stagingId'", status_code=400
        )

    import shutil

    from scenecraft.db import update_keyframe

    staging_file = pdir / "staging" / body.stagingId / f"v{body.variant}.png"
    if not staging_file.exists():
        raise ApiError(
            "NOT_FOUND",
            f"Staged candidate not found: {staging_file}",
            status_code=404,
        )

    sel_dir = pdir / "selected_keyframes"
    sel_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(staging_file), str(sel_dir / f"{body.keyframeId}.png"))

    cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{body.keyframeId}"
    cand_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(staging_file), str(cand_dir / f"v{body.variant}.png"))

    update_keyframe(
        pdir,
        body.keyframeId,
        selected=body.variant,
        candidates=[
            f"keyframe_candidates/candidates/section_{body.keyframeId}/v{body.variant}.png"
        ],
    )
    return {"success": True, "keyframeId": body.keyframeId}


# ---------------------------------------------------------------------------
# POST /generate-staged-candidate
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/generate-staged-candidate",
    operation_id="generate_staged_candidate",
    summary="Generate a keyframe image into staging/ without mutating the timeline.",
)
async def generate_staged_candidate(
    name: str, body: GenerateStagedBody, pdir: Path = Depends(project_dir)
) -> dict:
    """Kicks off a background generation job; returns jobId immediately.

    Behaviour mirrors ``_handle_generate_staged_candidate`` in legacy —
    the real work happens on a daemon thread inside ``job_manager``.
    """
    if not body.prompt or not body.stillName or not body.stagingId:
        raise ApiError(
            "BAD_REQUEST",
            "Missing 'prompt', 'stillName', or 'stagingId'",
            status_code=400,
        )

    source = pdir / "assets" / "stills" / body.stillName
    if not source.exists():
        raise ApiError(
            "NOT_FOUND", f"Still not found: {body.stillName}", status_code=404
        )

    from scenecraft.ws_server import job_manager

    job_id = job_manager.create_job(
        "staged_candidate",
        total=body.count,
        meta={"stagingId": body.stagingId, "project": name},
    )

    def _run():  # pragma: no cover — background thread, timing-dependent
        try:
            from scenecraft.db import get_meta as _get_meta_stg
            from scenecraft.render.google_video import GoogleVideoClient

            client = GoogleVideoClient(vertex=True)
            _img_model = _get_meta_stg(pdir).get("image_model", "replicate/nano-banana-2")

            from scenecraft.api.utils import _next_variant

            staging_dir = pdir / "staging" / body.stagingId
            staging_dir.mkdir(parents=True, exist_ok=True)

            existing = _next_variant(staging_dir, ".png") - 1
            paths: list[str] = []
            for i in range(body.count):
                v = existing + i + 1
                out_path = str(staging_dir / f"v{v}.png")
                if Path(out_path).exists():
                    paths.append(f"staging/{body.stagingId}/v{v}.png")
                    continue
                varied = f"{body.prompt}, variation {v}" if v > 1 else body.prompt
                try:
                    client.stylize_image(str(source), varied, out_path, image_model=_img_model)
                    paths.append(f"staging/{body.stagingId}/v{v}.png")
                    job_manager.update_progress(job_id, i + 1, f"v{v} done")
                except Exception as e:  # noqa: BLE001
                    job_manager.update_progress(job_id, i + 1, f"v{v} failed: {e}")

            all_paths = sorted(
                [f"staging/{body.stagingId}/{f.name}" for f in staging_dir.glob("v*.png")]
            )
            job_manager.complete_job(
                job_id, {"stagingId": body.stagingId, "candidates": all_paths}
            )
        except Exception as e:  # noqa: BLE001
            job_manager.fail_job(job_id, str(e))

    import threading

    threading.Thread(target=_run, daemon=True).start()
    return {"jobId": job_id, "stagingId": body.stagingId}
