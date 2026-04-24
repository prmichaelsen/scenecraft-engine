"""Chat-tool handler + right-click-operation handler for the transcribe plugin.

Both paths funnel into `ai.transcriber.transcribe_clip`. The handler only
extracts arguments, invokes the service, and shapes a chat-friendly return.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scenecraft.ai import transcriber
from scenecraft.ai.whisper_client import model_choices


TRANSCRIBE_CLIP_TOOL_DESCRIPTION = (
    "Transcribe an audio_clip to text via Whisper on Replicate. Caches by "
    "(clip_id, model, word_timestamps) — identical re-calls hit cache and "
    "are free. Results persist in transcribe__runs + transcribe__segments. "
    "Non-destructive. Returns run_id, model, full text, segment count, "
    "detected language, and duration."
)


TRANSCRIBE_CLIP_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clip_id": {
            "type": "string",
            "description": "audio_clips.id to transcribe.",
        },
        "model": {
            "type": "string",
            "enum": model_choices(),
            "description": "Override the plugin default_model for this call.",
        },
        "language": {
            "type": "string",
            "description": "ISO language code (e.g. 'en', 'es', 'ja'). Omit or '' for auto-detect.",
        },
        "word_timestamps": {
            "type": "boolean",
            "description": "Return per-word timestamps. Overrides plugin default_word_timestamps.",
        },
        "force_rerun": {
            "type": "boolean",
            "description": "Skip the cache and run fresh even if a matching run exists.",
        },
    },
    "required": ["clip_id"],
}


def _preview(text: str, *, max_chars: int = 500) -> str:
    """Compact text preview for the chat tool return — avoids shipping the
    entire transcript inline in the tool_result (Claude still sees it via
    the cached run via sql_query if needed)."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def handle_transcribe_clip(args: dict, context: dict) -> dict:
    """Chat-tool path. Returns a small summary + run_id."""
    project_dir: Path = context["project_dir"]
    clip_id = args.get("clip_id")
    if not clip_id or not isinstance(clip_id, str):
        return {"error": "clip_id is required and must be a string"}

    model = args.get("model")
    if model is not None and model not in model_choices():
        return {
            "error": f"unknown model {model!r}; valid options: {', '.join(model_choices())}"
        }

    language = args.get("language")
    word_timestamps = args.get("word_timestamps")
    force_rerun = bool(args.get("force_rerun", False))

    try:
        result = transcriber.transcribe_clip(
            project_dir,
            clip_id,
            model=model,
            language=language,
            word_timestamps=word_timestamps,
            force_rerun=force_rerun,
        )
    except FileNotFoundError as exc:
        return {"error": f"audio source missing on disk: {exc}"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "run_id": result.run_id,
        "clip_id": result.clip_id,
        "model": result.model,
        "model_slug": result.model_slug,
        "language": result.language,
        "word_timestamps": result.word_timestamps,
        "duration_seconds": result.duration_seconds,
        "segment_count": len(result.segments),
        "text_preview": _preview(result.text),
        "cached": result.cached,
    }


LIST_TRANSCRIPTIONS_TOOL_DESCRIPTION = (
    "List completed transcription runs for the current project. Optionally "
    "filter by clip_id to see every run against one audio source (e.g., to "
    "compare different Whisper model outputs). Returns a lightweight summary "
    "per run — call `transcribe__get_transcription` for the full text + "
    "segments of a specific run."
)

LIST_TRANSCRIPTIONS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clip_id": {
            "type": "string",
            "description": "Optional: limit results to runs of this audio_clip or pool_segment id.",
        },
    },
    "required": [],
}


GET_TRANSCRIPTION_TOOL_DESCRIPTION = (
    "Fetch one transcription run by run_id, including every segment with its "
    "start/end times, text, and (if word_timestamps was on at run time) "
    "per-word timing. Use this after `transcribe__transcribe_clip` or "
    "`transcribe__list_transcriptions` to read back full content for "
    "summarisation, lyric extraction, timeline alignment, etc."
)

GET_TRANSCRIPTION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_id": {
            "type": "string",
            "description": "ID of a transcribe__runs row (returned by transcribe_clip / list_transcriptions).",
        },
    },
    "required": ["run_id"],
}


def handle_list_transcriptions(args: dict, context: dict) -> dict:
    """Chat-tool path — lightweight browse of completed runs."""
    project_dir: Path = context["project_dir"]
    clip_id = args.get("clip_id")
    try:
        runs = transcriber.list_runs(project_dir, clip_id=clip_id if clip_id else None)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"runs": runs, "count": len(runs)}


def handle_get_transcription(args: dict, context: dict) -> dict:
    """Chat-tool path — read one run with all its segments."""
    project_dir: Path = context["project_dir"]
    run_id = args.get("run_id")
    if not run_id or not isinstance(run_id, str):
        return {"error": "run_id is required and must be a string"}
    try:
        result = transcriber.get_run(project_dir, run_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    if result is None:
        return {"error": f"transcribe run not found: {run_id}"}
    return {
        "run_id": result.run_id,
        "clip_id": result.clip_id,
        "model": result.model,
        "model_slug": result.model_slug,
        "language": result.language,
        "word_timestamps": result.word_timestamps,
        "duration_seconds": result.duration_seconds,
        "created_at": result.created_at,
        "text": result.text,
        "segments": [
            {
                "start": s.start,
                "end": s.end,
                "text": s.text,
                "words": [
                    {"text": w.text, "start": w.start, "end": w.end, "score": w.score}
                    for w in s.words
                ] if s.words else [],
            }
            for s in result.segments
        ],
    }


def handle_transcribe_operation(entity_type: str, entity_id: str, op_context: dict) -> dict:
    """Right-click-menu path. Reuses the same entry point.

    `op_context` comes from PluginHost.dispatch_rest-style invocation —
    expected to include at least `project_dir`. Extra args like
    `model` / `word_timestamps` are pulled from op_context if the frontend
    surfaces them via the context-menu modal; otherwise the plugin's
    default settings apply.
    """
    if entity_type != "audio_clip":
        return {"error": f"transcribe.run only supports audio_clip, got {entity_type!r}"}
    project_dir: Path = op_context["project_dir"]
    model = op_context.get("model")
    language = op_context.get("language")
    word_timestamps = op_context.get("word_timestamps")
    force_rerun = bool(op_context.get("force_rerun", False))

    try:
        result = transcriber.transcribe_clip(
            project_dir,
            entity_id,
            model=model,
            language=language,
            word_timestamps=word_timestamps,
            force_rerun=force_rerun,
        )
    except FileNotFoundError as exc:
        return {"error": f"audio source missing on disk: {exc}"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "run_id": result.run_id,
        "clip_id": result.clip_id,
        "model": result.model,
        "segments": [
            {
                "start": s.start,
                "end": s.end,
                "text": s.text,
                "words": [
                    {"text": w.text, "start": w.start, "end": w.end, "score": w.score}
                    for w in s.words
                ],
            }
            for s in result.segments
        ],
        "text": result.text,
        "language": result.language,
        "cached": result.cached,
    }
