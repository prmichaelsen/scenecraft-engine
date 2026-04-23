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
    duration: int               # seconds
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


def musicful_generate(payload: dict) -> list[str]:
    """POST /v1/music/generate — returns task_ids list.

    Musicful's /generate endpoint accepts a discriminated `action` union and
    returns task ids for the queued songs. Shape per spec's extracted docs.
    """
    response = plugin_api.call_service(
        service="musicful",
        method="POST",
        path="/v1/music/generate",
        body=payload,
        timeout_seconds=30.0,
    )
    body = response.body
    # Musicful responses vary; look for task_ids / ids / tasks / etc.
    if isinstance(body, dict):
        for key in ("task_ids", "ids", "tasks", "data"):
            val = body.get(key)
            if isinstance(val, list):
                # Coerce to strings since Musicful ids can be int or str.
                return [str(v.get("id") if isinstance(v, dict) else v) for v in val]
        # Single-id response
        if "id" in body:
            return [str(body["id"])]
        if "task_id" in body:
            return [str(body["task_id"])]
    raise ValueError(f"Unexpected Musicful /generate response shape: {type(body).__name__}")


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
    raw_songs = body if isinstance(body, list) else body.get("data", []) if isinstance(body, dict) else []
    songs: list[Song] = []
    for r in raw_songs:
        if not isinstance(r, dict):
            continue
        songs.append(
            Song(
                id=str(r.get("id") or r.get("task_id") or ""),
                title=r.get("title"),
                style=r.get("style"),
                duration=int(r.get("duration") or 0),
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
