"""Mix-render-upload + bounce-upload + bounce download + DSP/descriptions.

M16 T62 Phase 3 audio endpoints. Multipart parsing uses FastAPI's
``UploadFile + File(...)`` + ``Form(...)`` so ``python-multipart`` does
the RFC 7578 parsing rather than our hand-rolled boundary splitter.

No ``project_lock`` — these are cache-population routes (content-addressable
writes under ``pool/mixes/`` and ``pool/bounces/``), not timeline structural
mutations.

The bounce download (``GET /bounces/{id}.wav``) deliberately does NOT use
Range streaming: the legacy ``_handle_bounce_download`` sends the whole
file with ``Content-Disposition: attachment`` so browsers always treat it
as a download, never as an inline media element. Preserving that header
matters for the frontend's "Download bounce" button.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from starlette.responses import FileResponse

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError
from scenecraft.api.models.audio import (
    GenerateDescriptionsBody,
    GenerateDspBody,
)


router = APIRouter(prefix="/api/projects", tags=["audio"], dependencies=[Depends(current_user)])


_HEX_CHARS = set("0123456789abcdefABCDEF")


def _is_hex64(s: str) -> bool:
    return len(s) == 64 and all(c in _HEX_CHARS for c in s)


# ---------------------------------------------------------------------------
# POST /mix-render-upload (multipart)
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/mix-render-upload",
    operation_id="mix_render_upload",
    status_code=201,
)
async def mix_render_upload(
    name: str,
    audio: UploadFile = File(...),
    mix_graph_hash: str = Form(...),
    start_time_s: float = Form(...),
    end_time_s: float = Form(...),
    sample_rate: int = Form(...),
    channels: int = Form(...),
    request_id: str | None = Form(default=None),
    pd: Path = Depends(project_dir),
) -> dict:
    audio_data = await audio.read()
    if not audio_data:
        raise ApiError("BAD_REQUEST", "Missing 'audio' file", status_code=400)

    if not _is_hex64(mix_graph_hash):
        raise ApiError("BAD_REQUEST", "mix_graph_hash must be 64 hex chars", status_code=400)
    if channels not in (1, 2):
        raise ApiError("BAD_REQUEST", f"channels must be 1 or 2, got {channels}", status_code=400)
    if sample_rate <= 0:
        raise ApiError(
            "BAD_REQUEST", f"sample_rate must be positive, got {sample_rate}", status_code=400
        )
    if end_time_s <= start_time_s:
        raise ApiError(
            "BAD_REQUEST", "end_time_s must be > start_time_s", status_code=400
        )

    expected_duration = end_time_s - start_time_s

    mixes_dir = pd / "pool" / "mixes"
    mixes_dir.mkdir(parents=True, exist_ok=True)
    rel_path = f"pool/mixes/{mix_graph_hash}.wav"
    dest = pd / rel_path
    dest.write_bytes(audio_data)

    try:
        import wave

        with wave.open(str(dest), "rb") as wf:
            wav_channels = wf.getnchannels()
            wav_sample_rate = wf.getframerate()
            wav_frames = wf.getnframes()
        wav_duration = (
            wav_frames / float(wav_sample_rate) if wav_sample_rate > 0 else 0.0
        )
    except Exception as we:
        try:
            dest.unlink()
        except Exception:
            pass
        raise ApiError("BAD_REQUEST", f"Invalid WAV file: {we}", status_code=400)

    if wav_channels != channels:
        try:
            dest.unlink()
        except Exception:
            pass
        raise ApiError(
            "BAD_REQUEST",
            f"channels mismatch: form says {channels}, WAV header says {wav_channels}",
            status_code=400,
        )
    if wav_sample_rate != sample_rate:
        try:
            dest.unlink()
        except Exception:
            pass
        raise ApiError(
            "BAD_REQUEST",
            f"sample_rate mismatch: form says {sample_rate}, WAV header says {wav_sample_rate}",
            status_code=400,
        )
    if abs(wav_duration - expected_duration) > 0.100:
        try:
            dest.unlink()
        except Exception:
            pass
        raise ApiError(
            "BAD_REQUEST",
            f"duration mismatch: WAV is {wav_duration:.3f}s but end-start={expected_duration:.3f}s "
            "(>100ms drift)",
            status_code=400,
        )

    bytes_written = dest.stat().st_size

    released = False
    if request_id:
        try:
            from scenecraft.chat import set_mix_render_event

            released = set_mix_render_event(request_id)
        except Exception:
            pass

    return {
        "rendered_path": rel_path,
        "bytes": bytes_written,
        "channels": wav_channels,
        "sample_rate": wav_sample_rate,
        "duration_s": wav_duration,
        "chat_released": released,
    }


# ---------------------------------------------------------------------------
# POST /bounce-upload (multipart)
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/bounce-upload",
    operation_id="bounce_upload",
    status_code=201,
)
async def bounce_upload(
    name: str,
    audio: UploadFile = File(...),
    composite_hash: str = Form(...),
    start_time_s: float = Form(...),
    end_time_s: float = Form(...),
    sample_rate: int = Form(...),
    bit_depth: int = Form(...),
    channels: int = Form(...),
    request_id: str | None = Form(default=None),
    pd: Path = Depends(project_dir),
) -> dict:
    audio_data = await audio.read()
    if not audio_data:
        raise ApiError("BAD_REQUEST", "Missing 'audio' file", status_code=400)
    if not _is_hex64(composite_hash):
        raise ApiError("BAD_REQUEST", "composite_hash must be 64 hex chars", status_code=400)
    if channels not in (1, 2):
        raise ApiError("BAD_REQUEST", f"channels must be 1 or 2, got {channels}", status_code=400)
    if sample_rate <= 0:
        raise ApiError(
            "BAD_REQUEST", f"sample_rate must be positive, got {sample_rate}", status_code=400
        )
    if bit_depth not in (16, 24, 32):
        raise ApiError(
            "BAD_REQUEST", f"bit_depth must be 16, 24, or 32; got {bit_depth}", status_code=400
        )
    if end_time_s <= start_time_s:
        raise ApiError("BAD_REQUEST", "end_time_s must be > start_time_s", status_code=400)

    bounces_dir = pd / "pool" / "bounces"
    bounces_dir.mkdir(parents=True, exist_ok=True)
    rel_path = f"pool/bounces/{composite_hash}.wav"
    dest = pd / rel_path
    dest.write_bytes(audio_data)

    # Validate WAV header with ``wave``; fall back to ``soundfile`` for 32-bit
    # floats. Match legacy parity bit-for-bit.
    try:
        import wave

        with wave.open(str(dest), "rb") as wf:
            wav_channels = wf.getnchannels()
            wav_sample_rate = wf.getframerate()
            wav_frames = wf.getnframes()
        wav_duration = (
            wav_frames / float(wav_sample_rate) if wav_sample_rate > 0 else 0.0
        )
    except Exception:
        try:
            import soundfile as _sf

            info = _sf.info(str(dest))
            wav_channels = int(info.channels)
            wav_sample_rate = int(info.samplerate)
            wav_duration = float(info.duration)
        except Exception as we:
            try:
                dest.unlink()
            except Exception:
                pass
            raise ApiError("BAD_REQUEST", f"Invalid WAV file: {we}", status_code=400)

    if wav_channels != channels:
        try:
            dest.unlink()
        except Exception:
            pass
        raise ApiError(
            "BAD_REQUEST",
            f"channels mismatch: form says {channels}, WAV header says {wav_channels}",
            status_code=400,
        )
    if wav_sample_rate != sample_rate:
        try:
            dest.unlink()
        except Exception:
            pass
        raise ApiError(
            "BAD_REQUEST",
            f"sample_rate mismatch: form says {sample_rate}, WAV header says {wav_sample_rate}",
            status_code=400,
        )

    bytes_written = dest.stat().st_size

    released = False
    if request_id:
        try:
            from scenecraft.chat import set_bounce_render_event

            released = set_bounce_render_event(request_id)
        except Exception:
            pass

    return {
        "rendered_path": rel_path,
        "bytes": bytes_written,
        "channels": wav_channels,
        "sample_rate": wav_sample_rate,
        "bit_depth": bit_depth,
        "duration_s": wav_duration,
        "chat_released": released,
    }


# ---------------------------------------------------------------------------
# GET /bounces/{id}.wav
# ---------------------------------------------------------------------------


@router.get(
    "/{name}/bounces/{bounce_id}.wav",
    operation_id="download_bounce",
    responses={200: {"content": {"audio/wav": {}}}, 404: {}},
)
async def download_bounce(
    name: str,
    bounce_id: str,
    pd: Path = Depends(project_dir),
) -> FileResponse:
    from scenecraft.db_bounces import get_bounce_by_id

    try:
        bounce = get_bounce_by_id(pd, bounce_id)
    except Exception as e:
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    if bounce is None:
        raise ApiError("NOT_FOUND", f"Bounce not found: {bounce_id}", status_code=404)

    rel = bounce.rendered_path or f"pool/bounces/{bounce.composite_hash}.wav"
    full_path = (pd / rel).resolve()
    if not str(full_path).startswith(str(pd.resolve())):
        raise ApiError("FORBIDDEN", "Path traversal denied", status_code=403)
    if not full_path.exists():
        raise ApiError(
            "NOT_FOUND", f"Bounce WAV file missing on disk: {rel}", status_code=404
        )

    filename = f"{name}-{bounce_id}.wav"
    # Legacy behavior: no Range support — clients pulling a bounce want the
    # entire WAV for a local download, not video-element scrubbing. FileResponse
    # does emit Accept-Ranges when asked, but we keep the attachment disposition
    # header so browsers trigger the download dialog rather than inline-playing.
    return FileResponse(
        full_path,
        media_type="audio/wav",
        filename=filename,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ---------------------------------------------------------------------------
# POST /dsp/generate (🔧 chat-tool: generate_dsp)
# POST /descriptions/generate (🔧 chat-tool: generate_descriptions)
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/dsp/generate",
    operation_id="generate_dsp",
)
async def generate_dsp(
    name: str,
    body: GenerateDspBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.chat import _exec_generate_dsp

    payload = {
        "source_segment_id": body.source_segment_id,
        "force_rerun": body.force_rerun,
    }
    if body.analyses is not None:
        payload["analyses"] = body.analyses
    result = _exec_generate_dsp(pd, payload)
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", str(result["error"]), status_code=400)
    return result


@router.post(
    "/{name}/descriptions/generate",
    operation_id="generate_descriptions",
)
async def generate_descriptions(
    name: str,
    body: GenerateDescriptionsBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.chat import _exec_generate_descriptions

    payload: dict = {
        "source_segment_id": body.source_segment_id,
        "force_rerun": body.force_rerun,
    }
    if body.model is not None:
        payload["model"] = body.model
    if body.chunk_size_s is not None:
        payload["chunk_size_s"] = body.chunk_size_s
    if body.prompt_version is not None:
        payload["prompt_version"] = body.prompt_version
    result = _exec_generate_descriptions(pd, payload)
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", str(result["error"]), status_code=400)
    return result
