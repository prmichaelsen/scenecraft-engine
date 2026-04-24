"""Whisper transcription via Replicate HTTP API — no SDK dependency.

Supports four Whisper variants on Replicate, each with its own input schema
and output shape. The client handles the per-model differences and returns
a single `NormalizedTranscript` dataclass regardless of which model ran, so
callers downstream (cache, CLI, chat tool) see a consistent surface.

Pattern mirrors `render/kling_video.py` — raw `urllib.request`, Bearer auth
via `REPLICATE_API_TOKEN`, polling on `urls.get` until status transitions
out of `starting` / `processing`.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [whisper] {msg}", file=sys.stderr, flush=True)


REPLICATE_API = "https://api.replicate.com/v1"


# ── Normalized output shapes ─────────────────────────────────────────────


@dataclass
class TranscriptWord:
    """Optional per-word timestamp (WhisperX / whisper-timestamped only)."""
    text: str
    start: float
    end: float
    score: float | None = None


@dataclass
class TranscriptSegment:
    """A time-bounded chunk of transcript. All models emit these; `words`
    is populated only when the model + `word_timestamps` both cooperate."""
    start: float
    end: float
    text: str
    words: list[TranscriptWord] = field(default_factory=list)


@dataclass
class NormalizedTranscript:
    """Provider-agnostic shape callers see."""
    text: str
    segments: list[TranscriptSegment]
    language: str | None
    model: str                 # the alias ('fast', 'whisperx', ...)
    model_slug: str            # the Replicate model slug
    duration_seconds: float | None
    raw_output: Any            # the provider's JSON payload, kept for cache / debug


# ── Model registry ───────────────────────────────────────────────────────


def _build_input_fast(audio_uri: str, language: str | None, word_timestamps: bool) -> dict:
    """incredibly-fast-whisper — batched large-v3-turbo on Replicate."""
    body: dict[str, Any] = {
        "audio": audio_uri,
        "task": "transcribe",
        "return_timestamps": True,
        "batch_size": 24,
        "timestamp": "word" if word_timestamps else "chunk",
    }
    if language:
        body["language"] = language
    return body


def _parse_output_fast(output: Any) -> NormalizedTranscript:
    """incredibly-fast-whisper returns:
        { "text": "...",
          "chunks": [{ "timestamp": [start, end], "text": "..." }, ...] }
    With `timestamp: "word"` the chunks are individual words; with "chunk"
    they are phrase-length groups."""
    if not isinstance(output, dict):
        raise RuntimeError(f"fast-whisper: expected dict output, got {type(output).__name__}")
    text = output.get("text", "") or ""
    chunks = output.get("chunks", []) or []
    segments: list[TranscriptSegment] = []
    for c in chunks:
        ts = c.get("timestamp") or [None, None]
        try:
            start = float(ts[0]) if ts[0] is not None else 0.0
            end = float(ts[1]) if ts[1] is not None else start
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        segments.append(TranscriptSegment(start=start, end=end, text=(c.get("text") or "").strip()))
    return NormalizedTranscript(
        text=text.strip(),
        segments=segments,
        language=None,
        model="fast",
        model_slug=MODELS["fast"]["slug"],
        duration_seconds=(segments[-1].end if segments else None),
        raw_output=output,
    )


def _build_input_whisperx(audio_uri: str, language: str | None, word_timestamps: bool) -> dict:
    body: dict[str, Any] = {
        "audio_file": audio_uri,
        "align_output": word_timestamps,
    }
    if language:
        body["language"] = language
    return body


def _parse_output_whisperx(output: Any) -> NormalizedTranscript:
    """victor-upmeet/whisperx returns:
        { "detected_language": "en",
          "segments": [{ "start": 0.0, "end": 3.4, "text": "...",
                         "words": [{ "word": "hi", "start": 0.0, "end": 0.2, "score": 0.98 }]
                      }, ...] }"""
    if not isinstance(output, dict):
        raise RuntimeError(f"whisperx: expected dict output, got {type(output).__name__}")
    segments_raw = output.get("segments", []) or []
    segments: list[TranscriptSegment] = []
    parts: list[str] = []
    for s in segments_raw:
        try:
            start = float(s.get("start", 0.0))
            end = float(s.get("end", 0.0))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        txt = (s.get("text") or "").strip()
        words: list[TranscriptWord] = []
        for w in (s.get("words") or []):
            try:
                ws = float(w.get("start", start))
                we = float(w.get("end", end))
            except (TypeError, ValueError):
                ws, we = start, end
            words.append(TranscriptWord(
                text=(w.get("word") or "").strip(),
                start=ws, end=we,
                score=w.get("score"),
            ))
        segments.append(TranscriptSegment(start=start, end=end, text=txt, words=words))
        parts.append(txt)
    return NormalizedTranscript(
        text=" ".join(parts).strip(),
        segments=segments,
        language=output.get("detected_language"),
        model="whisperx",
        model_slug=MODELS["whisperx"]["slug"],
        duration_seconds=(segments[-1].end if segments else None),
        raw_output=output,
    )


def _build_input_whisper(audio_uri: str, language: str | None, word_timestamps: bool) -> dict:
    """openai/whisper on Replicate. Doesn't support word-level timestamps
    natively; `word_timestamps` is honoured by requesting per-word output
    if the specific model version supports it (newer revisions do)."""
    body: dict[str, Any] = {
        "audio": audio_uri,
        "model": "large-v3",
        "translate": False,
    }
    if language:
        body["language"] = language
    if word_timestamps:
        body["word_timestamps"] = True
    return body


def _parse_output_whisper(output: Any) -> NormalizedTranscript:
    """openai/whisper returns:
        { "detected_language": "...",
          "transcription": "full text",
          "segments": [{ "start": s, "end": e, "text": "..." }, ...] }"""
    if not isinstance(output, dict):
        raise RuntimeError(f"openai/whisper: expected dict output, got {type(output).__name__}")
    text = output.get("transcription") or output.get("text") or ""
    segments_raw = output.get("segments", []) or []
    segments: list[TranscriptSegment] = []
    for s in segments_raw:
        try:
            start = float(s.get("start", 0.0))
            end = float(s.get("end", 0.0))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        segments.append(TranscriptSegment(
            start=start, end=end,
            text=(s.get("text") or "").strip(),
        ))
    return NormalizedTranscript(
        text=(text or "").strip(),
        segments=segments,
        language=output.get("detected_language"),
        model="whisper",
        model_slug=MODELS["whisper"]["slug"],
        duration_seconds=(segments[-1].end if segments else None),
        raw_output=output,
    )


def _build_input_whisper_timestamped(audio_uri: str, language: str | None, word_timestamps: bool) -> dict:
    body: dict[str, Any] = {"audio": audio_uri}
    if language:
        body["language"] = language
    return body


def _parse_output_whisper_timestamped(output: Any) -> NormalizedTranscript:
    """whisper-timestamped returns segments with nested words. Shape:
        { "text": "...",
          "segments": [{ "start": s, "end": e, "text": "...",
                         "words": [{ "text": "...", "start": s, "end": e, "confidence": c }] }] }"""
    if not isinstance(output, dict):
        raise RuntimeError(f"whisper-timestamped: expected dict output, got {type(output).__name__}")
    text = (output.get("text") or "").strip()
    segments: list[TranscriptSegment] = []
    for s in (output.get("segments") or []):
        try:
            start = float(s.get("start", 0.0))
            end = float(s.get("end", 0.0))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        words: list[TranscriptWord] = []
        for w in (s.get("words") or []):
            try:
                ws = float(w.get("start", start))
                we = float(w.get("end", end))
            except (TypeError, ValueError):
                ws, we = start, end
            words.append(TranscriptWord(
                text=(w.get("text") or "").strip(),
                start=ws, end=we,
                score=w.get("confidence"),
            ))
        segments.append(TranscriptSegment(
            start=start, end=end,
            text=(s.get("text") or "").strip(),
            words=words,
        ))
    return NormalizedTranscript(
        text=text,
        segments=segments,
        language=None,
        model="whisper-timestamped",
        model_slug=MODELS["whisper-timestamped"]["slug"],
        duration_seconds=(segments[-1].end if segments else None),
        raw_output=output,
    )


# Registry of supported models. Keys are aliases used in the plugin setting
# enum, CLI flag, and chat tool schema. Adding a new Replicate-hosted model
# is one entry here plus a `_build_input_*` / `_parse_output_*` pair.
MODELS: dict[str, dict[str, Any]] = {
    "fast": {
        "slug": "vaibhavs10/incredibly-fast-whisper",
        "label": "Fast (large-v3-turbo)",
        "supports_word_timestamps": True,
        "build_input": _build_input_fast,
        "parse_output": _parse_output_fast,
    },
    "whisperx": {
        "slug": "victor-upmeet/whisperx",
        "label": "WhisperX (forced-alignment word timestamps)",
        "supports_word_timestamps": True,
        "build_input": _build_input_whisperx,
        "parse_output": _parse_output_whisperx,
    },
    "whisper": {
        "slug": "openai/whisper",
        "label": "OpenAI Whisper (classic)",
        "supports_word_timestamps": True,
        "build_input": _build_input_whisper,
        "parse_output": _parse_output_whisper,
    },
    "whisper-timestamped": {
        "slug": "openai/whisper-timestamped",
        "label": "Whisper Timestamped (word-level)",
        "supports_word_timestamps": True,
        "build_input": _build_input_whisper_timestamped,
        "parse_output": _parse_output_whisper_timestamped,
    },
}


def model_choices() -> list[str]:
    """Alias list suitable for enum schemas and `--model` CLI choices."""
    return list(MODELS.keys())


def resolve_model(alias: str) -> dict[str, Any]:
    """Raise a friendly error if an unknown alias sneaks past a schema."""
    cfg = MODELS.get(alias)
    if cfg is None:
        raise ValueError(
            f"unknown whisper model: {alias!r}. "
            f"Valid: {', '.join(MODELS.keys())}"
        )
    return cfg


# ── Client ──────────────────────────────────────────────────────────────


class WhisperClient:
    """Transcribe audio clips via Whisper models on Replicate."""

    def __init__(self, api_token: str | None = None):
        token = api_token or os.environ.get("REPLICATE_API_TOKEN")
        if not token:
            raise ValueError(
                "REPLICATE_API_TOKEN environment variable is required.\n"
                "Get a token at: https://replicate.com/account/api-tokens"
            )
        self.token = token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _post(self, url: str, data: dict) -> dict:
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    def _audio_to_data_uri(self, audio_path: str | Path) -> str:
        path = Path(audio_path)
        mime, _ = mimetypes.guess_type(str(path))
        # Fallback to audio/mpeg — Replicate's Whisper wrappers accept a
        # variety of formats and auto-detect, so the mime hint is cosmetic.
        mime = mime or "audio/mpeg"
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f"data:{mime};base64,{b64}"

    def _wait_for_prediction(
        self,
        prediction: dict,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        """Poll prediction.urls.get until status exits starting/processing."""
        url = prediction["urls"]["get"]
        start = time.time()
        last_status = ""
        while time.time() - start < timeout:
            result = self._get(url)
            status = result.get("status", "")
            if status != last_status:
                _log(f"prediction {result.get('id', '')[:8]} status={status}")
                last_status = status
            if on_progress:
                try: on_progress(result)
                except Exception: pass
            if status == "succeeded":
                return result
            if status in ("failed", "canceled"):
                err = result.get("error") or f"status={status}"
                raise RuntimeError(f"Whisper prediction failed: {err}")
            time.sleep(poll_interval)
        raise TimeoutError(f"Whisper prediction timed out after {timeout}s")

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        model: str = "fast",
        language: str | None = None,
        word_timestamps: bool = False,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
        on_progress: Callable[[dict], None] | None = None,
    ) -> NormalizedTranscript:
        """Transcribe a local audio file via the chosen Replicate model."""
        cfg = resolve_model(model)
        _log(f"transcribe model={model} slug={cfg['slug']} path={audio_path}")
        audio_uri = self._audio_to_data_uri(audio_path)
        input_data = cfg["build_input"](audio_uri, language, word_timestamps)
        prediction = self._post(
            f"{REPLICATE_API}/models/{cfg['slug']}/predictions",
            {"input": input_data},
        )
        result = self._wait_for_prediction(
            prediction,
            poll_interval=poll_interval,
            timeout=timeout,
            on_progress=on_progress,
        )
        output = result.get("output")
        normalized = cfg["parse_output"](output)
        _log(
            f"transcribe done segments={len(normalized.segments)} "
            f"duration={normalized.duration_seconds}"
        )
        return normalized
