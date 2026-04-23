"""Query helpers for Phase 3 analysis cache tables.

Two table families:

- ``dsp_*`` — cached librosa output (facts). Cache key:
  ``(source_segment_id, analyzer_version, params_hash)``.
- ``audio_description*`` — cached LLM structured-description output (vibes).
  Cache key: ``(source_segment_id, model, prompt_version)``.

Helpers are intentionally thin. Callers that want raw cursors for bulk ops
can hit the connection directly; these return decoded dataclasses for the
common case.

Cache semantics: source segments are immutable (new audio → new pool_segment),
so cache lookup by the cache-key tuple is the whole story. Upgrading librosa
changes ``analyzer_version``; iterating a prompt bumps ``prompt_version``; both
produce new rows without evicting old ones.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from scenecraft.db import generate_id, get_db
from scenecraft.db_models import (
    AudioDescription,
    AudioDescriptionRun,
    AudioDescriptionScalar,
    DspAnalysisRun,
    DspDatapoint,
    DspScalar,
    DspSection,
)


# ── DSP runs ────────────────────────────────────────────────────────


def get_dsp_run(
    project_dir: Path,
    source_segment_id: str,
    analyzer_version: str,
    params_hash: str,
) -> DspAnalysisRun | None:
    """Return the cached DSP run matching the key, or None."""
    row = get_db(project_dir).execute(
        "SELECT * FROM dsp_analysis_runs "
        "WHERE source_segment_id = ? AND analyzer_version = ? AND params_hash = ?",
        (source_segment_id, analyzer_version, params_hash),
    ).fetchone()
    return _row_to_dsp_run(row) if row else None


def create_dsp_run(
    project_dir: Path,
    source_segment_id: str,
    analyzer_version: str,
    params_hash: str,
    analyses: list[str],
    created_at: str,
) -> DspAnalysisRun:
    """Insert a new DSP run row. Caller is responsible for the uniqueness
    check (use ``get_dsp_run`` first if you want cache-hit semantics)."""
    run_id = generate_id("dsp_run")
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO dsp_analysis_runs (id, source_segment_id, analyzer_version, "
        "params_hash, analyses_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, source_segment_id, analyzer_version, params_hash,
         json.dumps(analyses), created_at),
    )
    conn.commit()
    return DspAnalysisRun(
        id=run_id,
        source_segment_id=source_segment_id,
        analyzer_version=analyzer_version,
        params_hash=params_hash,
        analyses=list(analyses),
        created_at=created_at,
    )


def list_dsp_runs(project_dir: Path, source_segment_id: str) -> list[DspAnalysisRun]:
    rows = get_db(project_dir).execute(
        "SELECT * FROM dsp_analysis_runs WHERE source_segment_id = ? "
        "ORDER BY created_at DESC",
        (source_segment_id,),
    ).fetchall()
    return [_row_to_dsp_run(r) for r in rows]


def delete_dsp_run(project_dir: Path, run_id: str) -> None:
    """Cascade deletes datapoints, sections, scalars."""
    conn = get_db(project_dir)
    conn.execute("DELETE FROM dsp_analysis_runs WHERE id = ?", (run_id,))
    conn.commit()


# ── DSP datapoints (bulk insert + filtered query) ───────────────────


def bulk_insert_dsp_datapoints(
    project_dir: Path,
    run_id: str,
    datapoints: Iterable[tuple[str, float, float, dict[str, Any] | None]],
) -> int:
    """Insert many datapoints in one transaction.

    ``datapoints`` tuples are ``(data_type, time_s, value, extra_or_none)``.
    Returns count inserted.
    """
    conn = get_db(project_dir)
    rows = [
        (run_id, dt, t, v, json.dumps(extra) if extra is not None else None)
        for (dt, t, v, extra) in datapoints
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO dsp_datapoints "
        "(run_id, data_type, time_s, value, extra_json) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def query_dsp_datapoints(
    project_dir: Path,
    run_id: str,
    data_type: str,
    *,
    time_start: float | None = None,
    time_end: float | None = None,
) -> list[DspDatapoint]:
    """Return datapoints of ``data_type`` within an optional time window."""
    sql = "SELECT * FROM dsp_datapoints WHERE run_id = ? AND data_type = ?"
    params: list[Any] = [run_id, data_type]
    if time_start is not None:
        sql += " AND time_s >= ?"
        params.append(time_start)
    if time_end is not None:
        sql += " AND time_s <= ?"
        params.append(time_end)
    sql += " ORDER BY time_s"
    rows = get_db(project_dir).execute(sql, params).fetchall()
    return [_row_to_dsp_datapoint(r) for r in rows]


# ── DSP sections ────────────────────────────────────────────────────


def bulk_insert_dsp_sections(
    project_dir: Path,
    run_id: str,
    sections: Iterable[tuple[float, float, str, str | None, float | None]],
) -> int:
    """``sections`` tuples are ``(start_s, end_s, section_type, label, confidence)``."""
    conn = get_db(project_dir)
    rows = [(run_id, s, e, t, label, conf) for (s, e, t, label, conf) in sections]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO dsp_sections "
        "(run_id, start_s, end_s, section_type, label, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def query_dsp_sections(
    project_dir: Path,
    run_id: str,
    section_type: str | None = None,
) -> list[DspSection]:
    sql = "SELECT * FROM dsp_sections WHERE run_id = ?"
    params: list[Any] = [run_id]
    if section_type is not None:
        sql += " AND section_type = ?"
        params.append(section_type)
    sql += " ORDER BY start_s"
    rows = get_db(project_dir).execute(sql, params).fetchall()
    return [_row_to_dsp_section(r) for r in rows]


# ── DSP scalars ─────────────────────────────────────────────────────


def set_dsp_scalars(
    project_dir: Path,
    run_id: str,
    metrics: dict[str, float],
) -> None:
    """Upsert scalars keyed by ``metric`` within a run."""
    if not metrics:
        return
    conn = get_db(project_dir)
    conn.executemany(
        "INSERT OR REPLACE INTO dsp_scalars (run_id, metric, value) VALUES (?, ?, ?)",
        [(run_id, k, float(v)) for k, v in metrics.items()],
    )
    conn.commit()


def get_dsp_scalars(project_dir: Path, run_id: str) -> dict[str, float]:
    rows = get_db(project_dir).execute(
        "SELECT metric, value FROM dsp_scalars WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return {r["metric"]: r["value"] for r in rows}


# ── Audio description runs ──────────────────────────────────────────


def get_audio_description_run(
    project_dir: Path,
    source_segment_id: str,
    model: str,
    prompt_version: str,
) -> AudioDescriptionRun | None:
    row = get_db(project_dir).execute(
        "SELECT * FROM audio_description_runs "
        "WHERE source_segment_id = ? AND model = ? AND prompt_version = ?",
        (source_segment_id, model, prompt_version),
    ).fetchone()
    return _row_to_audio_description_run(row) if row else None


def create_audio_description_run(
    project_dir: Path,
    source_segment_id: str,
    model: str,
    prompt_version: str,
    chunk_size_s: float,
    created_at: str,
) -> AudioDescriptionRun:
    run_id = generate_id("desc_run")
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO audio_description_runs (id, source_segment_id, model, "
        "prompt_version, chunk_size_s, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, source_segment_id, model, prompt_version, chunk_size_s, created_at),
    )
    conn.commit()
    return AudioDescriptionRun(
        id=run_id,
        source_segment_id=source_segment_id,
        model=model,
        prompt_version=prompt_version,
        chunk_size_s=chunk_size_s,
        created_at=created_at,
    )


def list_audio_description_runs(project_dir: Path, source_segment_id: str) -> list[AudioDescriptionRun]:
    rows = get_db(project_dir).execute(
        "SELECT * FROM audio_description_runs WHERE source_segment_id = ? "
        "ORDER BY created_at DESC",
        (source_segment_id,),
    ).fetchall()
    return [_row_to_audio_description_run(r) for r in rows]


def delete_audio_description_run(project_dir: Path, run_id: str) -> None:
    """Cascade deletes descriptions + scalars."""
    conn = get_db(project_dir)
    conn.execute("DELETE FROM audio_description_runs WHERE id = ?", (run_id,))
    conn.commit()


# ── Audio descriptions (time-ranged properties) ─────────────────────


def bulk_insert_audio_descriptions(
    project_dir: Path,
    run_id: str,
    descriptions: Iterable[tuple[float, float, str, str | None, float | None, float | None, dict[str, Any] | None]],
) -> int:
    """``descriptions`` tuples are
    ``(start_s, end_s, property, value_text, value_num, confidence, raw_or_none)``."""
    conn = get_db(project_dir)
    rows = [
        (run_id, s, e, prop, vt, vn, conf, json.dumps(raw) if raw is not None else None)
        for (s, e, prop, vt, vn, conf, raw) in descriptions
    ]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO audio_descriptions "
        "(run_id, start_s, end_s, property, value_text, value_num, confidence, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def query_audio_descriptions(
    project_dir: Path,
    run_id: str,
    property: str | None = None,
    *,
    time_start: float | None = None,
    time_end: float | None = None,
) -> list[AudioDescription]:
    sql = "SELECT * FROM audio_descriptions WHERE run_id = ?"
    params: list[Any] = [run_id]
    if property is not None:
        sql += " AND property = ?"
        params.append(property)
    if time_start is not None:
        sql += " AND end_s >= ?"
        params.append(time_start)
    if time_end is not None:
        sql += " AND start_s <= ?"
        params.append(time_end)
    sql += " ORDER BY start_s"
    rows = get_db(project_dir).execute(sql, params).fetchall()
    return [_row_to_audio_description(r) for r in rows]


# ── Audio description scalars ───────────────────────────────────────


def set_audio_description_scalars(
    project_dir: Path,
    run_id: str,
    scalars: Iterable[tuple[str, str | None, float | None, float | None]],
) -> None:
    """``scalars`` tuples are ``(property, value_text, value_num, confidence)``."""
    conn = get_db(project_dir)
    rows = [(run_id, p, vt, vn, conf) for (p, vt, vn, conf) in scalars]
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO audio_description_scalars "
        "(run_id, property, value_text, value_num, confidence) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def get_audio_description_scalars(project_dir: Path, run_id: str) -> list[AudioDescriptionScalar]:
    rows = get_db(project_dir).execute(
        "SELECT * FROM audio_description_scalars WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return [_row_to_audio_description_scalar(r) for r in rows]


# ── Row decoders ────────────────────────────────────────────────────


def _row_to_dsp_run(row: sqlite3.Row) -> DspAnalysisRun:
    return DspAnalysisRun(
        id=row["id"],
        source_segment_id=row["source_segment_id"],
        analyzer_version=row["analyzer_version"],
        params_hash=row["params_hash"],
        analyses=json.loads(row["analyses_json"]) if row["analyses_json"] else [],
        created_at=row["created_at"],
    )


def _row_to_dsp_datapoint(row: sqlite3.Row) -> DspDatapoint:
    extra = json.loads(row["extra_json"]) if row["extra_json"] else None
    return DspDatapoint(
        run_id=row["run_id"],
        data_type=row["data_type"],
        time_s=float(row["time_s"]),
        value=float(row["value"]),
        extra=extra,
    )


def _row_to_dsp_section(row: sqlite3.Row) -> DspSection:
    return DspSection(
        run_id=row["run_id"],
        start_s=float(row["start_s"]),
        end_s=float(row["end_s"]),
        section_type=row["section_type"],
        label=row["label"],
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
    )


def _row_to_audio_description_run(row: sqlite3.Row) -> AudioDescriptionRun:
    return AudioDescriptionRun(
        id=row["id"],
        source_segment_id=row["source_segment_id"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        chunk_size_s=float(row["chunk_size_s"]),
        created_at=row["created_at"],
    )


def _row_to_audio_description(row: sqlite3.Row) -> AudioDescription:
    raw = json.loads(row["raw_json"]) if row["raw_json"] else None
    return AudioDescription(
        run_id=row["run_id"],
        start_s=float(row["start_s"]),
        end_s=float(row["end_s"]),
        property=row["property"],
        value_text=row["value_text"],
        value_num=float(row["value_num"]) if row["value_num"] is not None else None,
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        raw=raw,
    )


def _row_to_audio_description_scalar(row: sqlite3.Row) -> AudioDescriptionScalar:
    return AudioDescriptionScalar(
        run_id=row["run_id"],
        property=row["property"],
        value_text=row["value_text"],
        value_num=float(row["value_num"]) if row["value_num"] is not None else None,
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
    )


__all__ = [
    # DSP
    "get_dsp_run",
    "create_dsp_run",
    "list_dsp_runs",
    "delete_dsp_run",
    "bulk_insert_dsp_datapoints",
    "query_dsp_datapoints",
    "bulk_insert_dsp_sections",
    "query_dsp_sections",
    "set_dsp_scalars",
    "get_dsp_scalars",
    # Descriptions
    "get_audio_description_run",
    "create_audio_description_run",
    "list_audio_description_runs",
    "delete_audio_description_run",
    "bulk_insert_audio_descriptions",
    "query_audio_descriptions",
    "set_audio_description_scalars",
    "get_audio_description_scalars",
]
