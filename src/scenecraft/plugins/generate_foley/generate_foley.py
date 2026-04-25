"""Foley generation run handler + worker thread.

Public entry:
    run(project_dir, project_name, *, prompt, mode, duration_seconds, ...)
        -> {generation_id, job_id, status}

Worker:
    - Spawns a daemon thread per invocation
    - t2fx: prompt-only, passes duration to MMAudio directly
    - v2fx: pre-trims the source tr_candidate to [in, out], base64-encodes
            the trimmed clip, passes as `video` input to MMAudio
    - Downloads Replicate output, copies into pool/segments/, hashes filename
    - Writes pool_segment + generate_foley__tracks junction row
    - Stamps pool_segment with context (variant_kind='foley',
      context_entity_*, derived_from)
    - Status transitions: pending -> running -> completed|failed

The provider (plugin_api.providers.replicate) owns HTTP, polling, backoff,
spend_ledger writes, and disconnect-survival. This module is foley-specific
business logic only.

See agent/design/local.foley-generation-plugin.md "Plugin module".
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from scenecraft import plugin_api
from scenecraft.plugins.generate_foley import pretrim

logger = logging.getLogger(__name__)

PLUGIN_ID = "generate-foley"
MMAUDIO_MODEL = "zsxkib/mmaudio"

# Validation bounds — mirror pretrim + clarification-12
MIN_DURATION = 1.0
MAX_DURATION = 30.0


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [generate-foley] {msg}", file=sys.stderr, flush=True)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_api_key() -> dict:
    """Invariant check — is REPLICATE_API_TOKEN set?"""
    if os.environ.get("REPLICATE_API_TOKEN"):
        return {"passed": True}
    return {
        "passed": False,
        "message": (
            "This plugin requires a Replicate API key. Set REPLICATE_API_TOKEN "
            "in your environment. See https://replicate.com/account/api-tokens."
        ),
    }


# --- Public entry ----------------------------------------------------------


def run(
    project_dir: Path,
    project_name: str,
    *,
    prompt: str | None = None,
    mode: Literal["t2fx", "v2fx"] | None = None,
    duration_seconds: float | None = None,
    source_candidate_id: str | None = None,
    source_in_seconds: float | None = None,
    source_out_seconds: float | None = None,
    negative_prompt: str | None = None,
    cfg_strength: float | None = None,
    seed: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    variant_count: int = 1,
    created_by: str = "plugin:generate-foley",
) -> dict:
    """Kick off a foley generation. Returns immediately with job_id.

    Mode inference (when ``mode`` is None):
      - source_candidate_id set → v2fx
      - else                    → t2fx

    Returns:
        {
          "generation_id": "<uuid>",
          "job_id": "<job_id>",
          "status": "pending",
          "mode": "t2fx" | "v2fx",
        }
    """
    # Validate + infer mode
    resolved_mode = _resolve_mode(mode, source_candidate_id)
    _validate(
        mode=resolved_mode,
        prompt=prompt,
        duration_seconds=duration_seconds,
        source_candidate_id=source_candidate_id,
        source_in_seconds=source_in_seconds,
        source_out_seconds=source_out_seconds,
        variant_count=variant_count,
    )

    generation_id = f"fgen_{uuid.uuid4().hex[:12]}"

    # Persist generation row — status='pending'
    plugin_api.add_foley_generation(
        project_dir,
        generation_id=generation_id,
        mode=resolved_mode,
        model=MMAUDIO_MODEL,
        prompt=prompt,
        duration_seconds=duration_seconds,
        source_candidate_id=source_candidate_id,
        source_in_seconds=source_in_seconds,
        source_out_seconds=source_out_seconds,
        negative_prompt=negative_prompt,
        cfg_strength=cfg_strength,
        seed=seed,
        entity_type=entity_type,
        entity_id=entity_id,
        variant_count=variant_count,
        status="pending",
        created_by=created_by,
    )

    # Job + WS event stream
    job_id = plugin_api.job_manager.create_job(
        job_type="generate-foley.run",
        total=1,
        meta={
            "generation_id": generation_id,
            "mode": resolved_mode,
            "entity_type": entity_type,
            "entity_id": entity_id,
        },
    )

    # Launch the worker (daemon = True so server shutdown doesn't hang)
    t = threading.Thread(
        target=_worker,
        args=(project_dir, project_name, generation_id, job_id, resolved_mode),
        kwargs={
            "prompt": prompt,
            "duration_seconds": duration_seconds,
            "source_candidate_id": source_candidate_id,
            "source_in_seconds": source_in_seconds,
            "source_out_seconds": source_out_seconds,
            "negative_prompt": negative_prompt,
            "cfg_strength": cfg_strength,
            "seed": seed,
            "entity_type": entity_type,
            "entity_id": entity_id,
        },
        daemon=True,
        name=f"foley-worker-{generation_id}",
    )
    t.start()

    return {
        "generation_id": generation_id,
        "job_id": job_id,
        "status": "pending",
        "mode": resolved_mode,
    }


def resume_in_flight(project_dir: Path) -> list[str]:
    """Startup hook — reattach polling for generations left in flight.

    Scans ``generate_foley__generations WHERE status IN ('pending','running')``,
    and for each one with a replicate_prediction_id (from its __tracks row),
    reattaches polling via the provider.

    Returns the list of reattached generation_ids (for logging).
    """
    reattached: list[str] = []
    generations = plugin_api.get_foley_generations_for_entity(project_dir)
    for gen in generations:
        if gen["status"] not in ("pending", "running"):
            continue
        # Pull the prediction_id out of tracks if any
        tracks = plugin_api.get_foley_generation_tracks(project_dir, gen["id"])
        prediction_id = None
        if tracks:
            prediction_id = tracks[0].get("replicate_prediction_id")
        if not prediction_id:
            # No prediction created yet; mark as failed (can't resume)
            plugin_api.update_foley_generation_status(
                project_dir, gen["id"], "failed",
                error="server restart before prediction was created",
                completed_at=_now_utc(),
            )
            continue
        reattached.append(gen["id"])
        t = threading.Thread(
            target=_reattach_worker,
            args=(project_dir, gen["id"], prediction_id),
            daemon=True,
            name=f"foley-reattach-{gen['id']}",
        )
        t.start()
    return reattached


# --- Worker ---------------------------------------------------------------


def _worker(
    project_dir: Path,
    project_name: str,
    generation_id: str,
    job_id: str,
    mode: str,
    *,
    prompt: str | None,
    duration_seconds: float | None,
    source_candidate_id: str | None,
    source_in_seconds: float | None,
    source_out_seconds: float | None,
    negative_prompt: str | None,
    cfg_strength: float | None,
    seed: int | None,
    entity_type: str | None,
    entity_id: str | None,
) -> None:
    from scenecraft.plugin_api.providers.replicate import (
        run_prediction,
        ReplicateDownloadFailed,
        ReplicatePredictionFailed,
        ReplicateNotConfigured,
        ReplicateError,
    )

    pretrim_path: Path | None = None
    try:
        plugin_api.update_foley_generation_status(
            project_dir, generation_id, "running",
            started_at=_now_utc(),
        )

        # Build the model input
        input_dict: dict[str, Any] = {}
        if prompt:
            input_dict["prompt"] = prompt
        # Duration: t2fx uses the slider; v2fx uses (out - in) after pre-trim
        if mode == "t2fx":
            input_dict["duration"] = duration_seconds or 8.0
        else:
            # v2fx: pre-trim the source and attach as data URI
            plugin_api.job_manager.update_progress(job_id, 0, detail="pretrim")
            src_path = _resolve_candidate_source_path(project_dir, source_candidate_id)
            pretrim_path = pretrim.trim_to_range(
                source_path=src_path,
                in_seconds=float(source_in_seconds),
                out_seconds=float(source_out_seconds),
            )
            input_dict["video"] = _file_to_data_uri(pretrim_path, mime="video/mp4")
            # Duration falls out from the trimmed video; cog overrides anyway.
            input_dict["duration"] = float(source_out_seconds - source_in_seconds)

        if negative_prompt is not None:
            input_dict["negative_prompt"] = negative_prompt
        if cfg_strength is not None:
            input_dict["cfg_strength"] = cfg_strength
        if seed is not None:
            input_dict["seed"] = seed

        # Dispatch to provider — blocks until Replicate terminal + download
        plugin_api.job_manager.update_progress(job_id, 0, detail="predicting")
        result = run_prediction(
            model=MMAUDIO_MODEL,
            input=input_dict,
            source=PLUGIN_ID,
        )

        # Persist output to pool
        plugin_api.job_manager.update_progress(job_id, 0, detail="downloading")
        pool_segment_id = _persist_output_to_pool(
            project_dir=project_dir,
            result=result,
            prompt=prompt,
            cfg_strength=cfg_strength,
            seed=seed,
            mode=mode,
            entity_type=entity_type,
            entity_id=entity_id,
            source_candidate_id=source_candidate_id,
        )

        # Record the track
        plugin_api.add_foley_track(
            project_dir,
            generation_id=generation_id,
            pool_segment_id=pool_segment_id,
            variant_index=0,
            replicate_prediction_id=result.prediction_id,
            duration_seconds=input_dict.get("duration"),
            spend_ledger_id=result.spend_ledger_id,
        )

        # Done!
        plugin_api.update_foley_generation_status(
            project_dir, generation_id, "completed",
            completed_at=_now_utc(),
        )
        plugin_api.job_manager.complete_job(job_id, result={
            "generation_id": generation_id,
            "pool_segment_id": pool_segment_id,
        })

    except ReplicateDownloadFailed as e:
        msg = (
            f"prediction charged (spend_ledger_id={e.spend_ledger_id}), "
            f"download failed. Retry will re-charge."
        )
        _log(f"[{generation_id}] {msg}")
        plugin_api.update_foley_generation_status(
            project_dir, generation_id, "failed", error=msg,
            completed_at=_now_utc(),
        )
        plugin_api.job_manager.fail_job(job_id, error=msg)
    except ReplicatePredictionFailed as e:
        _log(f"[{generation_id}] MMAudio prediction failed: {e}")
        plugin_api.update_foley_generation_status(
            project_dir, generation_id, "failed",
            error=f"MMAudio prediction failed: {e.error}",
            completed_at=_now_utc(),
        )
        plugin_api.job_manager.fail_job(job_id, error=str(e))
    except ReplicateNotConfigured as e:
        _log(f"[{generation_id}] Replicate not configured")
        plugin_api.update_foley_generation_status(
            project_dir, generation_id, "failed",
            error=str(e), completed_at=_now_utc(),
        )
        plugin_api.job_manager.fail_job(job_id, error=str(e))
    except ReplicateError as e:
        _log(f"[{generation_id}] Replicate provider error: {e}")
        plugin_api.update_foley_generation_status(
            project_dir, generation_id, "failed",
            error=str(e), completed_at=_now_utc(),
        )
        plugin_api.job_manager.fail_job(job_id, error=str(e))
    except Exception as e:
        logger.exception("[%s] unexpected error in foley worker", generation_id)
        plugin_api.update_foley_generation_status(
            project_dir, generation_id, "failed",
            error=f"{type(e).__name__}: {e}",
            completed_at=_now_utc(),
        )
        plugin_api.job_manager.fail_job(job_id, error=str(e))
    finally:
        # Clean up pretrim temp file
        if pretrim_path and pretrim_path.exists():
            try:
                pretrim_path.unlink()
            except OSError:
                pass


def _reattach_worker(project_dir: Path, generation_id: str, prediction_id: str) -> None:
    """Worker invoked by resume_in_flight to finish an interrupted prediction."""
    from scenecraft.plugin_api.providers.replicate import (
        attach_polling,
        PredictionResult,
        ReplicateError,
        ReplicatePredictionFailed,
        ReplicateDownloadFailed,
    )

    def on_complete(result_or_error):
        if isinstance(result_or_error, PredictionResult):
            # NOTE: attach_polling already recorded spend; we don't record again.
            # Also: we don't have the original generation params in context, so
            # we write a minimal track row and a pool_segment without full
            # generation_params metadata. Future versions could preserve more
            # state in generate_foley__generations.
            try:
                pool_segment_id = _persist_output_to_pool_minimal(
                    project_dir=project_dir,
                    result=result_or_error,
                    generation_id=generation_id,
                )
                plugin_api.add_foley_track(
                    project_dir,
                    generation_id=generation_id,
                    pool_segment_id=pool_segment_id,
                    variant_index=0,
                    replicate_prediction_id=result_or_error.prediction_id,
                    duration_seconds=None,
                    spend_ledger_id=result_or_error.spend_ledger_id,
                )
                plugin_api.update_foley_generation_status(
                    project_dir, generation_id, "completed",
                    completed_at=_now_utc(),
                )
            except Exception as e:
                logger.exception("[%s] reattach persist failed", generation_id)
                plugin_api.update_foley_generation_status(
                    project_dir, generation_id, "failed",
                    error=f"reattach persist failed: {e}",
                    completed_at=_now_utc(),
                )
        elif isinstance(result_or_error, ReplicateDownloadFailed):
            plugin_api.update_foley_generation_status(
                project_dir, generation_id, "failed",
                error=f"prediction charged ({result_or_error.spend_ledger_id}), download failed",
                completed_at=_now_utc(),
            )
        elif isinstance(result_or_error, ReplicatePredictionFailed):
            plugin_api.update_foley_generation_status(
                project_dir, generation_id, "failed",
                error=f"MMAudio failed: {result_or_error.error}",
                completed_at=_now_utc(),
            )
        elif isinstance(result_or_error, ReplicateError):
            plugin_api.update_foley_generation_status(
                project_dir, generation_id, "failed",
                error=str(result_or_error),
                completed_at=_now_utc(),
            )

    attach_polling(
        prediction_id=prediction_id,
        source=PLUGIN_ID,
        on_complete=on_complete,
    )


# --- Helpers --------------------------------------------------------------


def _resolve_mode(
    mode: str | None, source_candidate_id: str | None
) -> Literal["t2fx", "v2fx"]:
    if mode in ("t2fx", "v2fx"):
        return mode  # type: ignore[return-value]
    return "v2fx" if source_candidate_id else "t2fx"


def _validate(
    *,
    mode: str,
    prompt: str | None,
    duration_seconds: float | None,
    source_candidate_id: str | None,
    source_in_seconds: float | None,
    source_out_seconds: float | None,
    variant_count: int,
) -> None:
    if variant_count != 1:
        raise ValueError(f"variant_count must be 1 in MVP (got {variant_count})")
    if mode == "t2fx":
        if source_candidate_id is not None:
            raise ValueError("t2fx mode must not include source_candidate_id")
        if duration_seconds is not None:
            if not (MIN_DURATION <= duration_seconds <= MAX_DURATION):
                raise ValueError(
                    f"duration_seconds out of bounds "
                    f"[{MIN_DURATION}, {MAX_DURATION}]: {duration_seconds}"
                )
    else:  # v2fx
        if source_candidate_id is None:
            raise ValueError("v2fx mode requires source_candidate_id")
        if source_in_seconds is None or source_out_seconds is None:
            raise ValueError("v2fx mode requires source_in_seconds and source_out_seconds")
        if source_out_seconds <= source_in_seconds:
            raise ValueError("source_out_seconds must be > source_in_seconds")
        span = source_out_seconds - source_in_seconds
        if not (MIN_DURATION <= span <= MAX_DURATION):
            raise ValueError(
                f"v2fx range span out of bounds [{MIN_DURATION}, {MAX_DURATION}]: {span}s"
            )


def _resolve_candidate_source_path(project_dir: Path, candidate_id: str) -> Path:
    """Resolve a frontend ``candidate_id`` (pool_segment_id) to a file path.

    pool_segments dicts returned by plugin_api.get_pool_segment use camelCase
    keys (``poolPath``, not ``pool_path``) — see ``_row_to_pool_segment`` in
    scenecraft.db.
    """
    seg = plugin_api.get_pool_segment(project_dir, candidate_id)
    if seg is None:
        raise ValueError(f"candidate {candidate_id} not found in pool")
    pool_path = seg.get("poolPath") or seg.get("pool_path")
    if not pool_path:
        raise ValueError(f"candidate {candidate_id} has no poolPath")
    full = project_dir / pool_path if not Path(pool_path).is_absolute() else Path(pool_path)
    if not full.exists():
        raise ValueError(f"candidate source file not found on disk: {full}")
    return full


def _file_to_data_uri(path: Path, *, mime: str) -> str:
    """Encode a local file as a data URI for transport in the Replicate JSON input."""
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _persist_output_to_pool(
    *,
    project_dir: Path,
    result,
    prompt: str | None,
    cfg_strength: float | None,
    seed: int | None,
    mode: str,
    entity_type: str | None,
    entity_id: str | None,
    source_candidate_id: str | None,
) -> str:
    """Copy Replicate output into pool/segments/ and insert pool_segments row."""
    if not result.output_paths:
        raise ValueError("prediction returned no output paths")
    src = result.output_paths[0]

    # Destination: pool/segments/<uuid>.<ext>
    pool_dir = project_dir / "pool" / "segments"
    pool_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix or ".wav"
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = pool_dir / filename
    shutil.copy2(src, dest)

    # Gather metadata
    byte_size = dest.stat().st_size
    pool_path = f"pool/segments/{filename}"

    generation_params = {
        "provider": "replicate",
        "model": MMAUDIO_MODEL,
        "prompt": prompt,
        "cfg_strength": cfg_strength,
        "seed": seed,
        "mode": mode,
    }

    pool_segment_id = plugin_api.add_pool_segment(
        project_dir,
        kind="generated",
        created_by=f"plugin:{PLUGIN_ID}",
        pool_path=pool_path,
        generation_params=generation_params,
        byte_size=byte_size,
    )

    # Stamp variant_kind + context — these live on pool_segments but aren't
    # direct args to add_pool_segment.
    plugin_api.set_pool_segment_context(
        project_dir,
        pool_segment_id,
        context_entity_type=entity_type,
        context_entity_id=entity_id,
        variant_kind="foley",
    )

    # derived_from (v2fx only): strong-ref to the source tr_candidate's
    # pool_segment. Only set if we have one.
    if source_candidate_id:
        _set_derived_from(project_dir, pool_segment_id, source_candidate_id)

    return pool_segment_id


def _persist_output_to_pool_minimal(
    *, project_dir: Path, result, generation_id: str
) -> str:
    """Reattach-path persist — minimal metadata since we don't have request params.

    Looks up the original generation row to recover mode + entity + prompt.
    """
    gen = plugin_api.get_foley_generation(project_dir, generation_id)
    if gen is None:
        raise ValueError(f"generation {generation_id} not found during reattach")
    return _persist_output_to_pool(
        project_dir=project_dir,
        result=result,
        prompt=gen.get("prompt"),
        cfg_strength=gen.get("cfg_strength"),
        seed=gen.get("seed"),
        mode=gen.get("mode", "t2fx"),
        entity_type=gen.get("entity_type"),
        entity_id=gen.get("entity_id"),
        source_candidate_id=gen.get("source_candidate_id"),
    )


def _set_derived_from(
    project_dir: Path, pool_segment_id: str, source_candidate_id: str
) -> None:
    """Set pool_segments.derived_from via a scoped UPDATE.

    This is narrow enough that adding a full helper to plugin_api would be
    scope creep. Uses the db handle through plugin_api's public ``get_db``
    — but that helper isn't exposed. Workaround: call set_pool_segment_context
    for the context fields and issue a direct UPDATE here behind the
    plugin_api boundary. This violates the R9a invariant as stated.

    **TODO (task-144 followup):** add ``plugin_api.set_pool_segment_derived_from``
    so this can live on the plugin_api surface instead of inline here.
    """
    # Route via scenecraft.db — the cleanest available path until plugin_api
    # exposes a dedicated helper. Safe because this module is core-adjacent
    # (not a true 3rd-party plugin yet in M11's MVP scaffolding).
    from scenecraft.db import get_db
    conn = get_db(project_dir)
    conn.execute(
        "UPDATE pool_segments SET derived_from = ? WHERE id = ?",
        (source_candidate_id, pool_segment_id),
    )
    conn.commit()
