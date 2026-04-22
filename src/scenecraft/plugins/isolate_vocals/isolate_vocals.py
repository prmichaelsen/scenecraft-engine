"""isolate-vocals: DFN3 vocal extraction + numpy residual → background.

The handler ``run()`` kicks off a worker thread that:

1. Decodes the source to a canonical mono 48 kHz WAV.
2. Feeds the canonical WAV through DFN3 → ``vocal`` stem.
3. Subtracts ``source - vocal`` in the time domain → ``background`` stem.
4. Registers both stems as fresh ``pool_segments`` rows, linked via
   ``isolation_stems`` under a single ``audio_isolations`` row, inside one
   undo group. The job manager streams progress throughout.

Called from two surfaces:

* ``POST /api/projects/:name/plugins/isolate-vocals/run`` — via
  ``PluginHost.dispatch_rest`` (``handle_rest`` wrapper).
* ``chat.py::_execute_tool`` — via ``PluginHost.get_operation("isolate-vocals.run")``
  then ``op.handler(entity_type, entity_id, context)`` (see task 105).
"""

from __future__ import annotations

import threading
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path


_STEM_VOCAL = "vocal"
_STEM_BACKGROUND = "background"


def run(entity_type: str, entity_id: str, context: dict) -> dict:
    """Kick off a DFN3 + residual isolation run.

    Returns ``{"isolation_id": str, "job_id": str}`` on successful kickoff, or
    ``{"error": str}`` on synchronous failure (unknown entity type, source
    missing, clip not found, etc.). The actual DFN3 inference + stem writes
    run in a background thread; callers poll the job manager for progress.

    context keys:
      - ``project_dir`` (Path)
      - ``project_name`` (str, optional)
      - ``range_mode`` ('full' | 'subset')
      - ``trim_in`` (float | None, meaningful only when range_mode='subset')
      - ``trim_out`` (float | None)
    """
    from scenecraft import plugin_api

    if entity_type not in ("audio_clip", "transition"):
        return {"error": f"unsupported entity_type: {entity_type}"}
    if entity_type == "transition":
        # MVP scope: the UX supports both, but the backend currently only
        # resolves audio_clip sources. Transitions need video→audio extraction
        # from the selected candidate and broke the task into a follow-up.
        return {"error": "transition source resolution not implemented (audio_clip only for MVP)"}

    project_dir: Path = context["project_dir"]
    project_name: str = context.get("project_name", "")
    range_mode: str = context.get("range_mode", "full")
    trim_in = context.get("trim_in")
    trim_out = context.get("trim_out")

    source_path = _resolve_source_path(project_dir, entity_type, entity_id)
    if source_path is None or not source_path.exists():
        return {"error": "source audio not found"}

    isolation_id = plugin_api.add_audio_isolation(
        project_dir,
        entity_type=entity_type,
        entity_id=entity_id,
        model="deepfilternet3",
        range_mode=range_mode,
        trim_in=trim_in,
        trim_out=trim_out,
    )
    job_id = plugin_api.job_manager.create_job(
        "isolate_vocals",
        total=100,
        meta={
            "isolationId": isolation_id,
            "entityType": entity_type,
            "entityId": entity_id,
            "project": project_name,
            "plugin": "isolate-vocals",
        },
    )

    def _work() -> None:
        try:
            plugin_api.update_audio_isolation_status(project_dir, isolation_id, "running")

            pool_dir = project_dir / "pool" / "segments"
            pool_dir.mkdir(parents=True, exist_ok=True)

            # 1. Stage + decode source to canonical mono 48kHz PCM.
            tmp_in = pool_dir / f"_tmp_isolate_in_{isolation_id}.wav"
            _extract_source_wav(source_path, tmp_in, range_mode, trim_in, trim_out)
            plugin_api.job_manager.update_progress(job_id, 20, "source decoded")

            # 2. DFN3 → vocal stem (same duration, speech-enhanced).
            from . import model as _model  # local import so tests can monkeypatch

            tmp_vocal = pool_dir / f"_tmp_isolate_vocal_{isolation_id}.wav"
            _model.denoise_wav(tmp_in, tmp_vocal)
            plugin_api.job_manager.update_progress(job_id, 65, "vocal extracted")

            # 3. Residual: background = source - vocal (time-domain subtraction).
            tmp_bg = pool_dir / f"_tmp_isolate_bg_{isolation_id}.wav"
            _subtract_audio_wav(tmp_in, tmp_vocal, tmp_bg)
            plugin_api.job_manager.update_progress(job_id, 80, "residual computed")

            # 4. Pre-generate UUIDs and rename tmp → final.
            vocal_seg_id = uuid.uuid4().hex
            bg_seg_id = uuid.uuid4().hex
            vocal_out = pool_dir / f"{vocal_seg_id}.wav"
            bg_out = pool_dir / f"{bg_seg_id}.wav"
            tmp_vocal.rename(vocal_out)
            tmp_bg.rename(bg_out)
            tmp_in.unlink(missing_ok=True)

            dur = _wav_duration_seconds(vocal_out)
            vocal_size = vocal_out.stat().st_size
            bg_size = bg_out.stat().st_size
            now_iso = datetime.now(timezone.utc).astimezone().isoformat()

            # 5. Register both stems as pool_segments + junction rows (one undo group).
            plugin_api.undo_begin(
                project_dir, f"Isolate vocals: {entity_type} {entity_id}"
            )
            _insert_pool_segment(
                project_dir,
                seg_id=vocal_seg_id,
                pool_path=f"pool/segments/{vocal_seg_id}.wav",
                duration=dur,
                byte_size=vocal_size,
                created_by="isolate-vocals",
                created_at=now_iso,
                label=f"isolate-vocals · {_STEM_VOCAL}",
                generation_params={
                    "plugin": "isolate-vocals",
                    "model": "deepfilternet3",
                    "stem_type": _STEM_VOCAL,
                    "isolation_id": isolation_id,
                    "source_entity_type": entity_type,
                    "source_entity_id": entity_id,
                    "range_mode": range_mode,
                    "trim_in": trim_in,
                    "trim_out": trim_out,
                },
            )
            _insert_pool_segment(
                project_dir,
                seg_id=bg_seg_id,
                pool_path=f"pool/segments/{bg_seg_id}.wav",
                duration=dur,
                byte_size=bg_size,
                created_by="isolate-vocals",
                created_at=now_iso,
                label=f"isolate-vocals · {_STEM_BACKGROUND}",
                generation_params={
                    "plugin": "isolate-vocals",
                    "model": "deepfilternet3",
                    "stem_type": _STEM_BACKGROUND,
                    "isolation_id": isolation_id,
                    "source_entity_type": entity_type,
                    "source_entity_id": entity_id,
                    "range_mode": range_mode,
                    "trim_in": trim_in,
                    "trim_out": trim_out,
                },
            )
            plugin_api.add_isolation_stem(
                project_dir, isolation_id, vocal_seg_id, _STEM_VOCAL
            )
            plugin_api.add_isolation_stem(
                project_dir, isolation_id, bg_seg_id, _STEM_BACKGROUND
            )
            plugin_api.update_audio_isolation_status(
                project_dir, isolation_id, "completed"
            )

            plugin_api.job_manager.complete_job(
                job_id,
                {
                    "isolation_id": isolation_id,
                    "stems": [
                        {
                            "stem_type": _STEM_VOCAL,
                            "pool_segment_id": vocal_seg_id,
                            "pool_path": f"pool/segments/{vocal_seg_id}.wav",
                        },
                        {
                            "stem_type": _STEM_BACKGROUND,
                            "pool_segment_id": bg_seg_id,
                            "pool_path": f"pool/segments/{bg_seg_id}.wav",
                        },
                    ],
                },
            )
        except Exception as e:  # noqa: BLE001 — log + mark failed + surface to job
            import sys
            import traceback

            print(f"[isolate-vocals] failed: {e}", file=sys.stderr)
            traceback.print_exc()
            try:
                plugin_api.update_audio_isolation_status(
                    project_dir, isolation_id, "failed", error=str(e)
                )
            except Exception:
                pass
            plugin_api.job_manager.fail_job(job_id, str(e))

    threading.Thread(target=_work, daemon=True).start()
    return {"isolation_id": isolation_id, "job_id": job_id}


def handle_rest(
    path: str, project_dir: Path, project_name: str, body: dict | None
) -> dict:
    """POST /api/projects/:name/plugins/isolate-vocals/run.

    Thin wrapper: unpack the JSON body, dispatch to ``run``. Returns whatever
    ``run`` returns. ``api_server.py`` wraps this via ``PluginHost.dispatch_rest``.
    """
    body = body or {}
    entity_type = body.get("entity_type") or "audio_clip"
    entity_id = body.get("entity_id")
    if not entity_id:
        return {"error": "missing entity_id"}
    context = {
        "project_dir": project_dir,
        "project_name": project_name,
        "range_mode": body.get("range_mode", "full"),
        "trim_in": body.get("trim_in"),
        "trim_out": body.get("trim_out"),
    }
    return run(entity_type, entity_id, context)


# ── Private helpers ──────────────────────────────────────────────────────


def _resolve_source_path(
    project_dir: Path, entity_type: str, entity_id: str
) -> Path | None:
    """Map (entity_type, entity_id) → absolute on-disk source path.

    For ``audio_clip``: consults ``get_audio_clip_effective_path`` which
    correctly resolves a selected-candidate pool_segment or falls back to the
    clip's native ``source_path``.
    """
    from scenecraft import plugin_api
    from scenecraft.db import get_audio_clips

    if entity_type == "audio_clip":
        # get_audio_clip_effective_path takes the full clip dict; fetch it.
        clip = next(
            (c for c in get_audio_clips(project_dir) if c.get("id") == entity_id),
            None,
        )
        if not clip:
            return None
        rel = plugin_api.get_audio_clip_effective_path(project_dir, clip)
        if not rel:
            return None
        return (project_dir / rel).resolve()

    # Transitions not supported yet (caller rejects earlier).
    return None


def _extract_source_wav(
    src: Path,
    out: Path,
    range_mode: str,
    trim_in: float | None,
    trim_out: float | None,
) -> None:
    """ffmpeg: mono 48 kHz PCM. For range_mode='subset', apply -ss/-to."""
    import subprocess

    cmd: list[str] = ["ffmpeg", "-y"]
    if range_mode == "subset" and trim_in is not None:
        cmd += ["-ss", f"{float(trim_in):.6f}"]
    cmd += ["-i", str(src)]
    if range_mode == "subset" and trim_out is not None:
        # -to is absolute when placed after -i without -copyts; translate to -t.
        start = float(trim_in or 0.0)
        dur = max(0.0, float(trim_out) - start)
        cmd += ["-t", f"{dur:.6f}"]
    cmd += ["-ac", "1", "-ar", "48000", "-sample_fmt", "s16", str(out)]
    subprocess.run(cmd, capture_output=True, check=True, timeout=600)


def _read_wav_s16_mono(p: Path) -> tuple[bytes, int, int]:
    """Return (raw PCM bytes, sample_rate, n_frames) for a 16-bit mono WAV."""
    with wave.open(str(p), "rb") as w:
        assert w.getnchannels() == 1, "expected mono WAV"
        assert w.getsampwidth() == 2, "expected 16-bit PCM WAV"
        sr = w.getframerate()
        n = w.getnframes()
        pcm = w.readframes(n)
    return pcm, sr, n


def _write_wav_s16_mono(p: Path, pcm: bytes, sr: int) -> None:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


def _subtract_audio_wav(src_wav: Path, vocal_wav: Path, out_wav: Path) -> None:
    """Write (src - vocal) as 16-bit PCM mono WAV at the source sample rate.

    Both inputs come out of ffmpeg → same sr, same length by construction. If
    DFN3 ever produces a different-length output we defensively truncate to
    the shorter of the two arrays rather than erroring out.
    """
    try:
        import numpy as np  # noqa: WPS433 — soft dep
    except ImportError as e:  # pragma: no cover — numpy is a base dep
        raise RuntimeError("numpy required for residual subtraction") from e

    src_pcm, src_sr, _ = _read_wav_s16_mono(src_wav)
    voc_pcm, voc_sr, _ = _read_wav_s16_mono(vocal_wav)
    # If DFN3 resampled on us, force-resample via ffmpeg rather than silently
    # producing garbage.
    if src_sr != voc_sr:
        raise RuntimeError(
            f"sample rate mismatch: source={src_sr} vocal={voc_sr}; "
            f"DFN3 output resampled unexpectedly"
        )
    src_arr = np.frombuffer(src_pcm, dtype=np.int16).astype(np.int32)
    voc_arr = np.frombuffer(voc_pcm, dtype=np.int16).astype(np.int32)
    n = min(src_arr.size, voc_arr.size)
    diff = np.clip(src_arr[:n] - voc_arr[:n], -32768, 32767).astype(np.int16)
    _write_wav_s16_mono(out_wav, diff.tobytes(), src_sr)


def _wav_duration_seconds(p: Path) -> float:
    with wave.open(str(p), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _insert_pool_segment(
    project_dir: Path,
    *,
    seg_id: str,
    pool_path: str,
    duration: float,
    byte_size: int,
    created_by: str,
    created_at: str,
    label: str,
    generation_params: dict,
) -> None:
    """Insert a ``pool_segments`` row with a caller-supplied UUID.

    The canonical ``add_pool_segment`` helper generates its own UUID; this
    plugin pre-generates UUIDs so it can rename temp files directly to the
    final pool_path before DB insertion (guarantees atomicity between the
    file landing and the row existing). We go through ``_retry_on_locked``
    for the same write-contention reasons as the other helpers.
    """
    import json

    from scenecraft.db import _retry_on_locked, get_db

    conn = get_db(project_dir)

    def _do() -> None:
        conn.execute(
            """INSERT INTO pool_segments
               (id, pool_path, kind, created_by, label,
                generation_params, created_at, duration_seconds, byte_size)
               VALUES (?, ?, 'generated', ?, ?, ?, ?, ?, ?)""",
            (
                seg_id,
                pool_path,
                created_by,
                label,
                json.dumps(generation_params),
                created_at,
                duration,
                byte_size,
            ),
        )
        conn.commit()

    _retry_on_locked(_do)
