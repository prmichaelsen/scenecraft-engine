"""Music generation run handler + polling worker.

Public entry: run(project_dir, project_name, ...) kicks off a generation.
Returns {generation_id, task_ids, job_id} or {error}.

Polling is box-driven (M16 Option 1 per spec): a daemon thread polls
Musicful's /tasks endpoint every 5s until all tasks reach terminal state.
Exponential backoff (1/2/4s, max 3 retries) on HTTP 429. All other HTTP
errors surface immediately as failed.

Per 2026-04-23 dev directive: auth context is optional; username/org
default to '' in the spend_ledger row until the auth milestone ships.
"""

from __future__ import annotations

import os
import sys
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scenecraft import plugin_api
from scenecraft.plugins.generate_music.client import (
    Song,
    musicful_generate,
    musicful_get_tasks,
    musicful_get_key_info,
)

PLUGIN_ID = "generate-music"
POLL_INTERVAL_SECONDS = 5.0
RATE_LIMIT_BACKOFF = [1.0, 2.0, 4.0]
DOWNLOAD_TIMEOUT_SECONDS = 60.0


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [generate-music] {msg}", file=sys.stderr, flush=True)


def check_api_key() -> dict:
    """Invariant check — is MUSICFUL_API_KEY set?

    Exposed as the plugin's invariant check function per plugin.yaml's
    `contributes.invariants`. Harness lands in M17; this is declared
    forward-compat.
    """
    if os.environ.get("MUSICFUL_API_KEY"):
        return {"passed": True}
    return {
        "passed": False,
        "message": "This plugin requires a Musicful API key. Please contact your administrator.",
    }


def _build_payload(
    *,
    action: str,
    style: str,
    model: str,
    instrumental: int,
    lyrics: str | None = None,
    title: str | None = None,
    gender: str | None = None,
) -> dict:
    """Filter outgoing Musicful payload per spec R13.

    action='auto'   → {style, instrumental, gender, model, action, mv}
    action='custom' → + {lyrics, title} (unless instrumental=1 drops lyrics)
    """
    payload: dict[str, Any] = {
        "action": action,
        "style": style,
        "instrumental": instrumental,
        "model": model,
        "mv": model,
    }
    if gender:
        payload["gender"] = gender
    if action == "custom":
        if title:
            payload["title"] = title
        if lyrics and not instrumental:
            payload["lyrics"] = lyrics
    return payload


def run(
    project_dir: Path,
    project_name: str,
    *,
    action: str,
    style: str,
    lyrics: str | None = None,
    title: str | None = None,
    instrumental: int = 1,
    gender: str | None = None,
    model: str = "MFV2.0",
    entity_type: str | None = None,
    entity_id: str | None = None,
    auth_context: dict | None = None,
    reused_from: str | None = None,
) -> dict:
    """Kick off a music generation. Returns a dict with one of:
    - Success: {generation_id, task_ids, job_id}
    - Error:   {error: str}
    """
    # ── Validation (spec R5, R13) ────────────────────────────────────
    if action not in ("auto", "custom"):
        return {"error": f"action '{action}' not supported in MVP; use 'auto' or 'custom'"}
    if not style or not style.strip():
        return {"error": "style is required"}
    if len(style) > 5000:
        return {"error": "style exceeds 5000 character limit"}
    if action == "custom" and not instrumental and not (lyrics and lyrics.strip()):
        return {"error": "custom action with instrumental=0 requires lyrics"}
    if title and len(title) > 80:
        return {"error": "title exceeds 80 character limit"}

    # ── API key check (spec R53) ─────────────────────────────────────
    key_check = check_api_key()
    if not key_check["passed"]:
        return {"error": key_check["message"]}

    # ── Create pending generation row ────────────────────────────────
    generation_id = f"gen_{uuid.uuid4().hex[:12]}"
    plugin_api.add_music_generation(
        project_dir,
        generation_id=generation_id,
        action=action,
        model=model,
        instrumental=instrumental,
        style=style,
        lyrics=lyrics,
        title=title,
        gender=gender,
        entity_type=entity_type,
        entity_id=entity_id,
        reused_from=reused_from,
        status="pending",
    )

    # ── Call Musicful /generate ─────────────────────────────────────
    payload = _build_payload(
        action=action, style=style, model=model, instrumental=instrumental,
        lyrics=lyrics, title=title, gender=gender,
    )
    try:
        task_ids = musicful_generate(payload)
    except plugin_api.ServiceConfigError as e:
        plugin_api.update_music_generation_status(project_dir, generation_id, "failed", error=str(e))
        return {"error": str(e)}
    except plugin_api.ServiceError as e:
        plugin_api.update_music_generation_status(
            project_dir, generation_id, "failed", error=f"musicful_http_{e.status}"
        )
        return {"error": f"Musicful returned HTTP {e.status}"}
    except Exception as e:
        plugin_api.update_music_generation_status(project_dir, generation_id, "failed", error=str(e))
        return {"error": str(e)}

    if not task_ids:
        plugin_api.update_music_generation_status(
            project_dir, generation_id, "failed", error="musicful returned no task ids"
        )
        return {"error": "Musicful did not return any task ids"}

    # ── Transition to running ────────────────────────────────────────
    plugin_api.update_music_generation_status(
        project_dir, generation_id, "running", task_ids=task_ids
    )

    # ── JobManager + polling worker ──────────────────────────────────
    job_id = plugin_api.job_manager.create_job(
        "generate_music",
        total=len(task_ids),
        meta={
            "generationId": generation_id,
            "entityType": entity_type,
            "entityId": entity_id,
            "project": project_name,
        },
    )

    # Normalize auth_context for the worker; dev-mode '' defaults are fine.
    auth = auth_context or {}
    worker_auth = {
        "username": auth.get("username", ""),
        "org": auth.get("org", ""),
        "api_key_id": auth.get("api_key_id"),
    }

    worker = threading.Thread(
        target=_poll_worker,
        args=(project_dir, generation_id, task_ids, job_id, worker_auth),
        daemon=True,
    )
    worker.start()

    _log(f"started generation {generation_id} with {len(task_ids)} tasks")
    return {"generation_id": generation_id, "task_ids": task_ids, "job_id": job_id}


def _poll_worker(
    project_dir: Path,
    generation_id: str,
    task_ids: list[str],
    job_id: str,
    auth_context: dict,
) -> None:
    """Poll Musicful every 5s until all tasks terminal. Spec R16-R21."""
    pending = set(task_ids)
    completed: dict[str, Song] = {}
    failed: dict[str, Song] = {}
    backoff_queue = list(RATE_LIMIT_BACKOFF)

    try:
        while pending:
            time.sleep(POLL_INTERVAL_SECONDS)
            try:
                songs = musicful_get_tasks(list(pending))
            except plugin_api.ServiceError as e:
                if e.status == 429:
                    if not backoff_queue:
                        _finalize_failed(
                            project_dir, generation_id, job_id,
                            "rate_limit_exceeded",
                        )
                        return
                    wait = backoff_queue.pop(0)
                    _log(f"429 rate limit — backoff {wait}s")
                    time.sleep(wait)
                    continue
                _finalize_failed(
                    project_dir, generation_id, job_id,
                    f"musicful_http_{e.status}",
                )
                return
            except plugin_api.ServiceTimeoutError as e:
                _log(f"poll timeout: {e} — retrying next cycle")
                continue
            except Exception as e:
                _log(f"poll error: {e}")
                _finalize_failed(project_dir, generation_id, job_id, str(e))
                return

            progressed = False
            for song in songs:
                if song.id not in pending:
                    continue  # duplicate / already observed
                if song.is_completed:
                    completed[song.id] = song
                    pending.discard(song.id)
                    progressed = True
                elif song.is_failed:
                    failed[song.id] = song
                    pending.discard(song.id)
                    progressed = True

            if progressed:
                plugin_api.job_manager.update_progress(
                    job_id,
                    completed=len(completed) + len(failed),
                    detail=f"{len(completed)}/{len(task_ids)} completed",
                )

        _finalize(project_dir, generation_id, job_id, task_ids, completed, failed, auth_context)
    except Exception as e:  # pragma: no cover — top-level safety net
        _log(f"worker crashed: {e}")
        _finalize_failed(project_dir, generation_id, job_id, f"worker_crash: {e}")


def _finalize(
    project_dir: Path,
    generation_id: str,
    job_id: str,
    task_ids: list[str],
    completed: dict[str, Song],
    failed: dict[str, Song],
    auth_context: dict,
) -> None:
    """Write pool_segments + generation_tracks + spend_ledger; finalize status."""
    gen = plugin_api.get_music_generation(project_dir, generation_id)
    if gen is None:
        _log(f"generation {generation_id} missing; skipping finalize")
        return

    entity_type = gen.get("entity_type")
    entity_id = gen.get("entity_id")

    pool_segment_ids: list[str] = []
    for song_id, song in completed.items():
        try:
            seg_id = _save_song(project_dir, song, entity_type, entity_id, gen)
        except Exception as e:
            _log(f"failed to save song {song_id}: {e}")
            failed[song_id] = song  # demote to failed
            continue
        pool_segment_ids.append(seg_id)
        plugin_api.add_generation_track(
            project_dir,
            generation_id=generation_id,
            pool_segment_id=seg_id,
            musicful_task_id=song_id,
            song_title=song.title,
            duration_seconds=float(song.duration) if song.duration else None,
            cover_url=song.cover_url,
        )
        # Context-aware candidate routing (spec R22-R25)
        if entity_type == "audio_clip" and entity_id:
            try:
                plugin_api.add_audio_candidate(
                    project_dir,
                    audio_clip_id=entity_id,
                    pool_segment_id=seg_id,
                    source="generated",
                )
            except Exception as e:
                _log(f"add_audio_candidate failed for {seg_id}: {e}")
        elif entity_type == "transition" and entity_id:
            try:
                # M16 music generations land on slot 0 by default — future
                # design may allow selecting a specific slot.
                plugin_api.add_tr_candidate(
                    project_dir,
                    transition_id=entity_id,
                    slot=0,
                    pool_segment_id=seg_id,
                    source="generated",
                )
            except Exception as e:
                _log(f"add_tr_candidate failed for {seg_id}: {e}")

    # ── Record spend for successful songs (spec R19) ────────────────
    if pool_segment_ids:
        try:
            plugin_api.record_spend(
                plugin_id=PLUGIN_ID,
                amount=len(pool_segment_ids),
                unit="credit",
                operation="generate-music.run",
                username=auth_context.get("username", ""),
                org=auth_context.get("org", ""),
                api_key_id=auth_context.get("api_key_id"),
                job_ref=generation_id,
                metadata={"task_ids": task_ids},
                source="local",
            )
        except Exception as e:
            _log(f"record_spend failed: {e}")

    # ── Finalize status (R20, R21) ──────────────────────────────────
    error_str: str | None = None
    if failed:
        reasons = [f"{s.id}: {s.fail_reason or 'unknown'}" for s in failed.values()]
        error_str = "; ".join(reasons)

    if not pool_segment_ids:
        # All failed → status=failed, no spend recorded, no pool_segments
        plugin_api.update_music_generation_status(
            project_dir, generation_id, "failed", error=error_str or "all tasks failed"
        )
        plugin_api.job_manager.fail_job(job_id, error=error_str or "all tasks failed")
    else:
        # Completed or partial → status=completed; error populated if partial
        plugin_api.update_music_generation_status(
            project_dir, generation_id, "completed", error=error_str,
        )
        plugin_api.job_manager.complete_job(
            job_id,
            result={"generation_id": generation_id, "pool_segment_ids": pool_segment_ids},
        )


def _finalize_failed(
    project_dir: Path,
    generation_id: str,
    job_id: str,
    error: str,
) -> None:
    plugin_api.update_music_generation_status(project_dir, generation_id, "failed", error=error)
    plugin_api.job_manager.fail_job(job_id, error=error)


def _save_song(
    project_dir: Path,
    song: Song,
    entity_type: str | None,
    entity_id: str | None,
    generation_row: dict,
) -> str:
    """Download mp3 and register as pool_segment. Returns the DB seg_id.

    The on-disk filename uses an independent UUID for collision-free naming;
    add_pool_segment generates its own seg_id which we return. Atomic write
    via <uuid>.mp3.tmp → rename.
    """
    if not song.audio_url:
        raise ValueError(f"song {song.id} has no audio_url")

    file_uuid = uuid.uuid4().hex
    pool_rel = f"segments/{file_uuid}.mp3"
    pool_abs = project_dir / "pool" / pool_rel
    pool_abs.parent.mkdir(parents=True, exist_ok=True)
    tmp_abs = pool_abs.with_suffix(".mp3.tmp")

    try:
        _download_file(song.audio_url, tmp_abs, timeout_seconds=DOWNLOAD_TIMEOUT_SECONDS)
    except Exception:
        if tmp_abs.exists():
            tmp_abs.unlink(missing_ok=True)
        raise
    tmp_abs.rename(pool_abs)

    generation_params = {
        "provider": "musicful",
        "model": generation_row.get("model"),
        "action": generation_row.get("action"),
        "style": generation_row.get("style"),
        "lyrics": generation_row.get("lyrics"),
        "task_id": song.id,
        "cover_url": song.cover_url,
        "song_title": song.title,
    }

    seg_id = plugin_api.add_pool_segment(
        project_dir,
        pool_path=pool_rel,
        kind="generated",
        created_by=f"plugin:{PLUGIN_ID}",
        original_filename=None,
        original_filepath=None,
        duration_seconds=float(song.duration) if song.duration else None,
        generation_params=generation_params,
    )
    # Stamp variant_kind + context (spec R10, R40, Q2.2 Option Y)
    plugin_api.set_pool_segment_context(
        project_dir,
        seg_id,
        context_entity_type=entity_type,
        context_entity_id=entity_id,
        variant_kind="music",
    )
    return seg_id


def _download_file(url: str, out_path: Path, timeout_seconds: float) -> None:
    """Stream-download url → out_path. Raises on non-2xx or timeout."""
    try:
        import httpx  # type: ignore[import-untyped]
        with httpx.stream("GET", url, timeout=timeout_seconds, follow_redirects=True) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
        return
    except ImportError:
        pass

    import urllib.request
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"download failed: HTTP {resp.status}")
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)


# ── Retry + list helpers for REST routes (task-130) ─────────────────

def retry_generation(
    project_dir: Path,
    project_name: str,
    failed_generation_id: str,
    *,
    auth_context: dict | None = None,
) -> dict:
    """Create a new generation with the same params as a failed one.
    New row has reused_from=<failed_id>. Original row is NOT mutated.
    """
    orig = plugin_api.get_music_generation(project_dir, failed_generation_id)
    if orig is None:
        return {"error": f"generation {failed_generation_id} not found"}
    if orig["status"] != "failed":
        return {"error": f"only failed generations may be retried (current status: {orig['status']})"}

    return run(
        project_dir, project_name,
        action=orig["action"],
        style=orig.get("style") or "",
        lyrics=orig.get("lyrics"),
        title=orig.get("title"),
        instrumental=orig["instrumental"],
        gender=orig.get("gender"),
        model=orig["model"],
        entity_type=orig.get("entity_type"),
        entity_id=orig.get("entity_id"),
        auth_context=auth_context,
        reused_from=failed_generation_id,
    )


def list_generations(
    project_dir: Path,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    return plugin_api.get_music_generations_for_entity(
        project_dir,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
    )


def get_credits() -> dict:
    """Lightweight credits check — calls Musicful /get_api_key_info."""
    key_check = check_api_key()
    if not key_check["passed"]:
        return {"credits": None, "error": key_check["message"]}
    try:
        info = musicful_get_key_info()
    except plugin_api.ServiceError as e:
        return {"credits": None, "error": f"Musicful returned HTTP {e.status}"}
    except Exception as e:
        return {"credits": None, "error": str(e)}
    credits_val = info.get("key_music_counts")
    try:
        credits_int = int(credits_val) if credits_val is not None else 0
    except (TypeError, ValueError):
        credits_int = 0
    return {
        "credits": credits_int,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
    }
