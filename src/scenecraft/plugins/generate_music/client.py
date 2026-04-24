"""Musicful REST client — thin wrapper over plugin_api.call_service.

Every HTTP call to Musicful routes through plugin_api.call_service, which
handles BYO auth (env var) and will handle brokered auth in a future
milestone. The plugin never holds the API key directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from scenecraft import plugin_api


@dataclass
class Song:
    """Parsed row from GET /v1/music/tasks — one per task id."""
    id: str                     # Musicful task id
    title: str | None
    style: str | None
    duration: float             # seconds (Musicful returns ms; we divide at parse time)
    audio_url: str | None
    cover_url: str | None
    status: int                 # Musicful status code (integer)
    song_id: str | None
    lyric: str | None
    fail_code: int | None
    fail_reason: str | None

    @property
    def is_terminal(self) -> bool:
        # Musicful status codes: ~2xx in-flight, ~3xx completed, 4xx failed.
        # Conservative mapping: terminal when audio_url set (success) or
        # fail_reason set (failure).
        return bool(self.audio_url) or bool(self.fail_reason) or (
            self.fail_code is not None and self.fail_code > 0
        )

    @property
    def is_completed(self) -> bool:
        return bool(self.audio_url) and not self.fail_reason

    @property
    def is_failed(self) -> bool:
        return bool(self.fail_reason) or (
            self.fail_code is not None and self.fail_code > 0
        )


SUPPORTED_MUSICFUL_MODELS = ("MFV2.0", "MFV1.5X", "MFV1.5", "MFV1.0")


def musicful_generate(payload: dict) -> list[str]:
    """POST /v1/music/generate — returns task_ids list.

    Musicful wraps every response in a ``{status, message, data}`` envelope.
    Success: ``{"status": 200, "message": "Success", "data": {"ids": [...]}}``.
    Error:   ``{"status": 4xxxxx, "message": "...", "data": {}}``.
    Pydantic validation errors come back in FastAPI's ``{"detail": [...]}``
    format when a field is rejected (e.g. unknown ``mv`` value).
    """
    response = plugin_api.call_service(
        service="musicful",
        method="POST",
        path="/v1/music/generate",
        body=payload,
        timeout_seconds=30.0,
    )
    body = response.body

    if not isinstance(body, dict):
        raise ValueError(f"Unexpected Musicful /generate response shape: {type(body).__name__}")

    # FastAPI validation error envelope
    if "detail" in body and "status" not in body:
        detail = body["detail"]
        if isinstance(detail, list) and detail:
            msgs = [d.get("msg", "") for d in detail if isinstance(d, dict)]
            raise ValueError(f"Musicful rejected request: {'; '.join(m for m in msgs if m)}")
        raise ValueError(f"Musicful rejected request: {detail}")

    # Standard envelope — surface the server-side message on non-success
    status = body.get("status")
    if status is not None and status != 200:
        message = body.get("message") or f"status={status}"
        raise ValueError(f"Musicful error {status}: {message}")

    # Success path — ids live under data (occasionally top-level in older shapes)
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    for key in ("task_ids", "ids", "tasks"):
        val = data.get(key) if isinstance(data, dict) else None
        if isinstance(val, list):
            return [str(v.get("id") if isinstance(v, dict) else v) for v in val]
    for key in ("id", "task_id"):
        if isinstance(data, dict) and key in data:
            return [str(data[key])]

    raise ValueError(f"Musicful /generate returned no task ids (body keys: {list(body.keys())})")


def musicful_get_tasks(task_ids: list[str]) -> list[Song]:
    """GET /v1/music/tasks?ids=... — returns Song objects.

    Query the task-details endpoint for one or more ids and parse.
    Musicful returns an array of song objects.
    """
    response = plugin_api.call_service(
        service="musicful",
        method="GET",
        path="/v1/music/tasks",
        query={"ids": ",".join(task_ids)},
        timeout_seconds=15.0,
    )
    body = response.body
    # Mirror /generate's envelope handling
    if isinstance(body, dict):
        status = body.get("status")
        if status is not None and status != 200:
            msg = body.get("message") or f"status={status}"
            raise ValueError(f"Musicful tasks error {status}: {msg}")
        data = body.get("data", body)
        raw_songs = data if isinstance(data, list) else (data.get("songs") or data.get("tasks") or []) if isinstance(data, dict) else []
    elif isinstance(body, list):
        raw_songs = body
    else:
        raw_songs = []
    songs: list[Song] = []
    for r in raw_songs:
        if not isinstance(r, dict):
            continue
        songs.append(
            Song(
                id=str(r.get("id") or r.get("task_id") or ""),
                title=r.get("title"),
                style=r.get("style"),
                # Musicful returns duration in milliseconds (e.g. 167920 = 2:48).
                # Normalize to seconds so pool_segments.duration_seconds stays honest.
                duration=(float(r.get("duration") or 0) / 1000.0) if (r.get("duration") or 0) > 0 else 0.0,
                audio_url=r.get("audio_url"),
                cover_url=r.get("cover_url"),
                status=int(r.get("status") or 0),
                song_id=r.get("song_id"),
                lyric=r.get("lyric"),
                fail_code=r.get("fail_code"),
                fail_reason=r.get("fail_reason"),
            )
        )
    return songs


def musicful_get_key_info() -> dict:
    """GET /v1/get_api_key_info — returns the key's quota + metadata.

    Relevant fields: key_music_counts (remaining credits), email, key_status.
    """
    response = plugin_api.call_service(
        service="musicful",
        method="GET",
        path="/v1/get_api_key_info",
        timeout_seconds=10.0,
    )
    return response.body if isinstance(response.body, dict) else {}
