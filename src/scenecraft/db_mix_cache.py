"""Query helpers for M15 master-bus mix analysis cache.

Cache key: ``(mix_graph_hash, start_time_s, end_time_s, sample_rate,
analyzer_version)``. The ``mix_graph_hash`` is computed by
``scenecraft.mix_graph_hash.compute_mix_graph_hash`` over every mix-affecting
factor (tracks, clips, effects, curves, sends). Any edit that changes the
rendered mix invalidates the cache entry.

Sibling to ``db_analysis_cache.py`` for the dsp_* + audio_description* tables;
mirrors those helpers' conventions. Rows decode into the dataclasses defined
in ``db_models.py``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from scenecraft.db import generate_id, get_db
from scenecraft.db_models import (
    MixAnalysisRun,
    MixDatapoint,
    MixScalar,
    MixSection,
)


# ── Mix runs ────────────────────────────────────────────────────────


def get_mix_run(
    project_dir: Path,
    mix_graph_hash: str,
    start_s: float,
    end_s: float,
    sample_rate: int,
    analyzer_version: str,
) -> MixAnalysisRun | None:
    """Return the cached mix run matching the full cache key, or None."""
    row = get_db(project_dir).execute(
        "SELECT * FROM mix_analysis_runs "
        "WHERE mix_graph_hash = ? AND start_time_s = ? AND end_time_s = ? "
        "AND sample_rate = ? AND analyzer_version = ?",
        (mix_graph_hash, start_s, end_s, sample_rate, analyzer_version),
    ).fetchone()
    return _row_to_mix_run(row) if row else None


def create_mix_run(
    project_dir: Path,
    mix_graph_hash: str,
    start_s: float,
    end_s: float,
    sample_rate: int,
    analyzer_version: str,
    analyses: list[str],
    rendered_path: str | None,
    created_at: str,
) -> MixAnalysisRun:
    """Insert a new mix_analysis_runs row. Caller checks uniqueness via
    ``get_mix_run`` first if they want cache-hit semantics."""
    run_id = generate_id("mix_run")
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO mix_analysis_runs (id, mix_graph_hash, start_time_s, "
        "end_time_s, sample_rate, analyzer_version, analyses_json, "
        "rendered_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, mix_graph_hash, start_s, end_s, sample_rate,
         analyzer_version, json.dumps(analyses), rendered_path, created_at),
    )
    conn.commit()
    return MixAnalysisRun(
        id=run_id,
        mix_graph_hash=mix_graph_hash,
        start_time_s=start_s,
        end_time_s=end_s,
        sample_rate=sample_rate,
        analyzer_version=analyzer_version,
        analyses=list(analyses),
        rendered_path=rendered_path,
        created_at=created_at,
    )


def update_mix_run_rendered_path(
    project_dir: Path, run_id: str, rendered_path: str,
) -> None:
    """Set ``rendered_path`` on an existing mix run (e.g. once the render +
    upload completes). No-op if the run does not exist."""
    conn = get_db(project_dir)
    conn.execute(
        "UPDATE mix_analysis_runs SET rendered_path = ? WHERE id = ?",
        (rendered_path, run_id),
    )
    conn.commit()


def list_mix_runs_for_hash(
    project_dir: Path, mix_graph_hash: str,
) -> list[MixAnalysisRun]:
    """All runs for a given mix_graph_hash, newest first."""
    rows = get_db(project_dir).execute(
        "SELECT * FROM mix_analysis_runs WHERE mix_graph_hash = ? "
        "ORDER BY created_at DESC",
        (mix_graph_hash,),
    ).fetchall()
    return [_row_to_mix_run(r) for r in rows]


def delete_mix_run(project_dir: Path, run_id: str) -> None:
    """Cascade deletes datapoints, sections, scalars."""
    conn = get_db(project_dir)
    conn.execute("DELETE FROM mix_analysis_runs WHERE id = ?", (run_id,))
    conn.commit()


# ── Mix datapoints (bulk insert + filtered query) ───────────────────


def bulk_insert_mix_datapoints(
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
        "INSERT OR REPLACE INTO mix_datapoints "
        "(run_id, data_type, time_s, value, extra_json) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def query_mix_datapoints(
    project_dir: Path,
    run_id: str,
    data_type: str,
    *,
    time_start: float | None = None,
    time_end: float | None = None,
) -> list[MixDatapoint]:
    """Return datapoints of ``data_type`` within an optional time window."""
    sql = "SELECT * FROM mix_datapoints WHERE run_id = ? AND data_type = ?"
    params: list[Any] = [run_id, data_type]
    if time_start is not None:
        sql += " AND time_s >= ?"
        params.append(time_start)
    if time_end is not None:
        sql += " AND time_s <= ?"
        params.append(time_end)
    sql += " ORDER BY time_s"
    rows = get_db(project_dir).execute(sql, params).fetchall()
    return [_row_to_mix_datapoint(r) for r in rows]


# ── Mix sections ────────────────────────────────────────────────────


def bulk_insert_mix_sections(
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
        "INSERT OR REPLACE INTO mix_sections "
        "(run_id, start_s, end_s, section_type, label, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def query_mix_sections(
    project_dir: Path,
    run_id: str,
    section_type: str | None = None,
) -> list[MixSection]:
    sql = "SELECT * FROM mix_sections WHERE run_id = ?"
    params: list[Any] = [run_id]
    if section_type is not None:
        sql += " AND section_type = ?"
        params.append(section_type)
    sql += " ORDER BY start_s"
    rows = get_db(project_dir).execute(sql, params).fetchall()
    return [_row_to_mix_section(r) for r in rows]


# ── Mix scalars ─────────────────────────────────────────────────────


def set_mix_scalars(
    project_dir: Path,
    run_id: str,
    metrics: dict[str, float],
) -> None:
    """Upsert scalars keyed by ``metric`` within a run."""
    if not metrics:
        return
    conn = get_db(project_dir)
    conn.executemany(
        "INSERT OR REPLACE INTO mix_scalars (run_id, metric, value) VALUES (?, ?, ?)",
        [(run_id, k, float(v)) for k, v in metrics.items()],
    )
    conn.commit()


def get_mix_scalars(project_dir: Path, run_id: str) -> dict[str, float]:
    rows = get_db(project_dir).execute(
        "SELECT metric, value FROM mix_scalars WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return {r["metric"]: r["value"] for r in rows}


# ── Row decoders ────────────────────────────────────────────────────


def _row_to_mix_run(row: sqlite3.Row) -> MixAnalysisRun:
    return MixAnalysisRun(
        id=row["id"],
        mix_graph_hash=row["mix_graph_hash"],
        start_time_s=float(row["start_time_s"]),
        end_time_s=float(row["end_time_s"]),
        sample_rate=int(row["sample_rate"]),
        analyzer_version=row["analyzer_version"],
        analyses=json.loads(row["analyses_json"]) if row["analyses_json"] else [],
        rendered_path=row["rendered_path"],
        created_at=row["created_at"],
    )


def _row_to_mix_datapoint(row: sqlite3.Row) -> MixDatapoint:
    extra = json.loads(row["extra_json"]) if row["extra_json"] else None
    return MixDatapoint(
        run_id=row["run_id"],
        data_type=row["data_type"],
        time_s=float(row["time_s"]),
        value=float(row["value"]),
        extra=extra,
    )


def _row_to_mix_section(row: sqlite3.Row) -> MixSection:
    return MixSection(
        run_id=row["run_id"],
        start_s=float(row["start_s"]),
        end_s=float(row["end_s"]),
        section_type=row["section_type"],
        label=row["label"],
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
    )


__all__ = [
    "get_mix_run",
    "create_mix_run",
    "update_mix_run_rendered_path",
    "list_mix_runs_for_hash",
    "delete_mix_run",
    "bulk_insert_mix_datapoints",
    "query_mix_datapoints",
    "bulk_insert_mix_sections",
    "query_mix_sections",
    "set_mix_scalars",
    "get_mix_scalars",
]
