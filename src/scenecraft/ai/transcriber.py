"""High-level transcribe_clip: plugin-settings resolution, cache lookup,
WhisperClient dispatch, persistence.

Writes into `transcribe__runs` + `transcribe__segments` (see db.py schema).
Caches by (clip_id, model, word_timestamps). Callers downstream (CLI, chat
tool, future UI) see the same `TranscribeRunResult` regardless of which
Whisper variant actually ran.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scenecraft.ai.whisper_client import (
    MODELS,
    NormalizedTranscript,
    TranscriptSegment,
    TranscriptWord,
    WhisperClient,
    model_choices,
)


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [transcribe] {msg}", file=sys.stderr, flush=True)


# ── Public result shape ─────────────────────────────────────────────────


@dataclass
class TranscribeRunResult:
    run_id: str
    clip_id: str
    model: str
    model_slug: str
    language: str | None
    word_timestamps: bool
    text: str
    segments: list[TranscriptSegment]
    duration_seconds: float | None
    cached: bool
    created_at: str


# ── Plugin-settings resolution ──────────────────────────────────────────
#
# Until a formal plugin_api.get_plugin_settings() lands, we stash plugin
# settings in the project's `meta` table under the `plugin_setting:<plugin>:`
# key prefix. All plugins follow this convention so settings survive
# project reload + appear in the existing version-control snapshot.


PLUGIN_ID = "transcribe"

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_model": "fast",
    "default_language": "",          # empty = auto-detect
    "default_word_timestamps": False,
}


def _settings_key(name: str) -> str:
    return f"plugin_setting:{PLUGIN_ID}:{name}"


def get_plugin_settings(project_dir: Path) -> dict[str, Any]:
    """Merge defaults with anything the user / host has written into meta."""
    from scenecraft.db import get_meta
    meta = get_meta(project_dir)
    resolved = dict(DEFAULT_SETTINGS)
    for name in DEFAULT_SETTINGS:
        stored = meta.get(_settings_key(name))
        if stored is not None:
            resolved[name] = stored
    return resolved


def set_plugin_setting(project_dir: Path, name: str, value: Any) -> None:
    if name not in DEFAULT_SETTINGS:
        raise ValueError(f"unknown transcribe setting: {name!r}")
    from scenecraft.db import set_meta
    set_meta(project_dir, _settings_key(name), value)


# ── Cache lookup ────────────────────────────────────────────────────────


def _find_cached_run(
    project_dir: Path,
    clip_id: str,
    model: str,
    word_timestamps: bool,
) -> str | None:
    """Return the most recent completed run_id matching the cache key, or None."""
    from scenecraft.db import get_db
    conn = get_db(project_dir)
    row = conn.execute(
        """SELECT id FROM transcribe__runs
           WHERE clip_id = ? AND model = ? AND word_timestamps = ? AND status = 'completed'
           ORDER BY created_at DESC
           LIMIT 1""",
        (clip_id, model, 1 if word_timestamps else 0),
    ).fetchone()
    return row["id"] if row else None


def list_runs(project_dir: Path, clip_id: str | None = None) -> list[dict]:
    """Return a chat/REST-friendly summary of every completed run.

    Lighter than loading each run with segments — callers use this to
    pick a run id, then fetch the full one via ``get_run``. Filters by
    ``clip_id`` when provided (matches transcribe_clip's cache-key
    semantics — any run referencing that clip, across models).
    """
    from scenecraft.db import get_db
    conn = get_db(project_dir)
    if clip_id:
        rows = conn.execute(
            """SELECT id, clip_id, model, model_slug, language, word_timestamps,
                      detected_language, duration_seconds, status, created_at,
                      substr(text, 1, 280) AS text_preview,
                      (SELECT COUNT(*) FROM transcribe__segments s WHERE s.run_id = r.id) AS segment_count
               FROM transcribe__runs r
               WHERE clip_id = ? AND status = 'completed'
               ORDER BY created_at DESC""",
            (clip_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, clip_id, model, model_slug, language, word_timestamps,
                      detected_language, duration_seconds, status, created_at,
                      substr(text, 1, 280) AS text_preview,
                      (SELECT COUNT(*) FROM transcribe__segments s WHERE s.run_id = r.id) AS segment_count
               FROM transcribe__runs r
               WHERE status = 'completed'
               ORDER BY created_at DESC
               LIMIT 200""",
        ).fetchall()
    return [
        {
            "run_id": r["id"],
            "clip_id": r["clip_id"],
            "model": r["model"],
            "model_slug": r["model_slug"],
            "language_requested": r["language"],
            "detected_language": r["detected_language"],
            "word_timestamps": bool(r["word_timestamps"]),
            "duration_seconds": r["duration_seconds"],
            "segment_count": r["segment_count"],
            "text_preview": r["text_preview"] or "",
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_run(project_dir: Path, run_id: str) -> TranscribeRunResult | None:
    """Fetch a full run with segments, or None if the id doesn't exist."""
    from scenecraft.db import get_db
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT 1 FROM transcribe__runs WHERE id = ?", (run_id,),
    ).fetchone()
    if row is None:
        return None
    return _load_run(project_dir, run_id)


def _load_run(project_dir: Path, run_id: str) -> TranscribeRunResult:
    from scenecraft.db import get_db
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT * FROM transcribe__runs WHERE id = ?", (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"transcribe run not found: {run_id}")
    seg_rows = conn.execute(
        """SELECT seq, start_seconds, end_seconds, text, words_json
           FROM transcribe__segments
           WHERE run_id = ?
           ORDER BY seq""",
        (run_id,),
    ).fetchall()
    segments: list[TranscriptSegment] = []
    for s in seg_rows:
        words: list[TranscriptWord] = []
        if s["words_json"]:
            try:
                for w in json.loads(s["words_json"]):
                    words.append(TranscriptWord(
                        text=w.get("text", ""),
                        start=float(w.get("start", 0.0)),
                        end=float(w.get("end", 0.0)),
                        score=w.get("score"),
                    ))
            except (ValueError, TypeError):
                pass
        segments.append(TranscriptSegment(
            start=float(s["start_seconds"]),
            end=float(s["end_seconds"]),
            text=s["text"] or "",
            words=words,
        ))
    return TranscribeRunResult(
        run_id=row["id"],
        clip_id=row["clip_id"],
        model=row["model"],
        model_slug=row["model_slug"],
        language=row["detected_language"],
        word_timestamps=bool(row["word_timestamps"]),
        text=row["text"] or "",
        segments=segments,
        duration_seconds=row["duration_seconds"],
        cached=True,
        created_at=row["created_at"],
    )


# ── Persist a completed run ─────────────────────────────────────────────


def _persist_run(
    project_dir: Path,
    clip_id: str,
    transcript: NormalizedTranscript,
    *,
    language_request: str | None,
    word_timestamps: bool,
    created_by: str = "",
) -> TranscribeRunResult:
    from scenecraft.db import get_db
    conn = get_db(project_dir)
    now = datetime.now(timezone.utc).isoformat()
    run_id = f"tr_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """INSERT INTO transcribe__runs
           (id, clip_id, model, model_slug, language, word_timestamps,
            text, detected_language, duration_seconds, status, error,
            raw_output, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', NULL, ?, ?, ?)""",
        (
            run_id,
            clip_id,
            transcript.model,
            transcript.model_slug,
            language_request or None,
            1 if word_timestamps else 0,
            transcript.text,
            transcript.language,
            transcript.duration_seconds,
            json.dumps(transcript.raw_output) if transcript.raw_output is not None else None,
            created_by,
            now,
        ),
    )
    for i, seg in enumerate(transcript.segments):
        words_json = None
        if seg.words:
            words_json = json.dumps([
                {"text": w.text, "start": w.start, "end": w.end, "score": w.score}
                for w in seg.words
            ])
        conn.execute(
            """INSERT INTO transcribe__segments
               (run_id, seq, start_seconds, end_seconds, text, words_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, i, seg.start, seg.end, seg.text, words_json),
        )
    conn.commit()
    _log(f"persisted run {run_id} ({len(transcript.segments)} segments)")
    return TranscribeRunResult(
        run_id=run_id,
        clip_id=clip_id,
        model=transcript.model,
        model_slug=transcript.model_slug,
        language=transcript.language,
        word_timestamps=word_timestamps,
        text=transcript.text,
        segments=transcript.segments,
        duration_seconds=transcript.duration_seconds,
        cached=False,
        created_at=now,
    )


# ── Clip lookup ─────────────────────────────────────────────────────────


def _resolve_clip_audio_path(project_dir: Path, clip_or_segment_id: str) -> Path:
    """Find the audio file for a given id.

    Checks ``audio_clips`` first (the timeline-placed sources the transcribe
    tool is shaped around); if no match, falls back to ``pool_segments`` so
    users can transcribe pool audio directly without first dragging it onto
    a timeline. In either case returns an absolute file path; raises
    ValueError if the id doesn't exist in either table, and FileNotFoundError
    if the resolved path isn't on disk.
    """
    from scenecraft.db import get_db
    conn = get_db(project_dir)

    # 1. Timeline-placed audio clip
    row = conn.execute(
        "SELECT source_path FROM audio_clips WHERE id = ? AND deleted_at IS NULL",
        (clip_or_segment_id,),
    ).fetchone()
    if row is not None:
        source = row["source_path"] or ""
        if not source:
            raise ValueError(f"audio_clip {clip_or_segment_id} has no source_path")
    else:
        # 2. Pool segment — transcribe the raw import without needing a
        #    timeline placement. `pool_segments.pool_path` is a relative
        #    path under the project dir (e.g. pool/segments/import_<uuid>.wav).
        pool_row = conn.execute(
            "SELECT pool_path FROM pool_segments WHERE id = ?",
            (clip_or_segment_id,),
        ).fetchone()
        if pool_row is None:
            raise ValueError(
                f"id not found in audio_clips or pool_segments: {clip_or_segment_id}"
            )
        source = pool_row["pool_path"] or ""
        if not source:
            raise ValueError(f"pool_segment {clip_or_segment_id} has no pool_path")

    candidate = Path(source)
    if not candidate.is_absolute():
        candidate = project_dir / source
    if not candidate.exists():
        raise FileNotFoundError(f"audio source not on disk: {candidate}")
    return candidate


# ── Public entrypoint ───────────────────────────────────────────────────


def transcribe_clip(
    project_dir: Path,
    clip_id: str,
    *,
    model: str | None = None,
    language: str | None = None,
    word_timestamps: bool | None = None,
    force_rerun: bool = False,
    created_by: str = "",
) -> TranscribeRunResult:
    """Transcribe an audio_clip. Provider-agnostic + cache-aware.

    Resolution order for optional args:
      1. Explicit arg (if not None)
      2. Plugin setting (`get_plugin_settings`)
      3. Hardcoded default

    Empty-string language requests collapse to None (auto-detect).
    """
    settings = get_plugin_settings(project_dir)

    resolved_model = model or settings.get("default_model") or "fast"
    if resolved_model not in MODELS:
        raise ValueError(f"unknown whisper model: {resolved_model!r} (valid: {model_choices()})")

    if language is None:
        language = settings.get("default_language") or ""
    resolved_language = language.strip() if isinstance(language, str) else None
    if not resolved_language:
        resolved_language = None

    if word_timestamps is None:
        word_timestamps = bool(settings.get("default_word_timestamps", False))
    resolved_words = bool(word_timestamps)

    # Cache lookup: the (clip, model, word_timestamps) triple must match. We
    # intentionally do NOT include `language` in the cache key — a cached
    # run done with auto-detect is still useful when the user later asks
    # for en-specific, and vice versa.
    if not force_rerun:
        cached_id = _find_cached_run(project_dir, clip_id, resolved_model, resolved_words)
        if cached_id:
            _log(f"cache hit run={cached_id} (clip={clip_id[:12]} model={resolved_model})")
            return _load_run(project_dir, cached_id)

    audio_path = _resolve_clip_audio_path(project_dir, clip_id)
    client = WhisperClient()
    transcript = client.transcribe(
        audio_path,
        model=resolved_model,
        language=resolved_language,
        word_timestamps=resolved_words,
    )
    return _persist_run(
        project_dir,
        clip_id,
        transcript,
        language_request=resolved_language,
        word_timestamps=resolved_words,
        created_by=created_by,
    )
