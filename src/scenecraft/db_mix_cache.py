"""STUB — to be replaced by sibling branch ``m15-mix-schema``.

This module will eventually provide the query helpers for the
``mix_analysis_runs`` / ``mix_datapoints`` / ``mix_sections`` / ``mix_scalars``
tables, analogous to ``db_analysis_cache.py`` for the DSP (per-segment)
family.

Cache key: ``(mix_graph_hash, start_time_s, end_time_s, sample_rate,
analyzer_version)`` — the rendered WAV is cached under
``pool/mixes/<mix_graph_hash>.wav`` and the analysis rows hang off the run.

This branch (``m15-analyze-master``) only exposes the chat tool
``analyze_master_bus``. The schema + table creation + real SQL belong to
the sibling branch. To keep this branch compiling and testable, we ship
these in-memory stubs with the same signatures the sibling will ship.

Tests for ``analyze_master_bus`` monkeypatch every symbol in this module so
nothing in the stubs needs to be production-correct — they only need to
parse and keep the tool's error paths out of the critical path.
"""

# TODO(M15): replace with real impl from sibling branch m15-mix-schema.

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass
class MixAnalysisRun:
    id: str
    mix_graph_hash: str
    start_time_s: float
    end_time_s: float
    sample_rate: int
    analyzer_version: str
    analyses: list[str] = field(default_factory=list)
    rendered_path: str | None = None
    created_at: str = ""


# In-memory bookkeeping shared across stub functions. The sibling branch
# will replace these with SQLite-backed equivalents; tests should not rely
# on this store's persistence across process boundaries.
_STUB_RUNS: dict[tuple[str, str, tuple[float, float, int, str]], MixAnalysisRun] = {}
_STUB_BY_ID: dict[tuple[str, str], MixAnalysisRun] = {}
_STUB_DATAPOINTS: dict[tuple[str, str], list[tuple[str, float, float, dict | None]]] = {}
_STUB_SECTIONS: dict[tuple[str, str], list[tuple[float, float, str, str | None, float | None]]] = {}
_STUB_SCALARS: dict[tuple[str, str], dict[str, float]] = {}


def _pkey(project_dir: Path) -> str:
    return str(Path(project_dir).resolve())


def get_mix_run(
    project_dir: Path,
    mix_graph_hash: str,
    start_s: float,
    end_s: float,
    sample_rate: int,
    analyzer_version: str,
) -> MixAnalysisRun | None:
    """Return the cached mix run matching the full cache key, or None."""
    key = (_pkey(project_dir), mix_graph_hash, (float(start_s), float(end_s), int(sample_rate), analyzer_version))
    return _STUB_RUNS.get(key)


def create_mix_run(
    project_dir: Path,
    *,
    mix_graph_hash: str,
    start_time_s: float,
    end_time_s: float,
    sample_rate: int,
    analyzer_version: str,
    analyses: list[str],
    rendered_path: str | None = None,
    created_at: str | None = None,
) -> MixAnalysisRun:
    """Insert a new mix run row. Caller handles uniqueness — call
    ``get_mix_run`` first for cache-hit semantics."""
    run = MixAnalysisRun(
        id=f"mix_run_{uuid.uuid4().hex[:12]}",
        mix_graph_hash=mix_graph_hash,
        start_time_s=float(start_time_s),
        end_time_s=float(end_time_s),
        sample_rate=int(sample_rate),
        analyzer_version=analyzer_version,
        analyses=list(analyses),
        rendered_path=rendered_path,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )
    pk = _pkey(project_dir)
    key = (pk, mix_graph_hash, (run.start_time_s, run.end_time_s, run.sample_rate, analyzer_version))
    _STUB_RUNS[key] = run
    _STUB_BY_ID[(pk, run.id)] = run
    return run


def update_mix_run_rendered_path(project_dir: Path, run_id: str, rendered_path: str | None) -> None:
    run = _STUB_BY_ID.get((_pkey(project_dir), run_id))
    if run is not None:
        run.rendered_path = rendered_path


def delete_mix_run(project_dir: Path, run_id: str) -> None:
    """Cascade-delete the run + its datapoints/sections/scalars."""
    pk = _pkey(project_dir)
    run = _STUB_BY_ID.pop((pk, run_id), None)
    if run is None:
        return
    key = (pk, run.mix_graph_hash, (run.start_time_s, run.end_time_s, run.sample_rate, run.analyzer_version))
    _STUB_RUNS.pop(key, None)
    _STUB_DATAPOINTS.pop((pk, run_id), None)
    _STUB_SECTIONS.pop((pk, run_id), None)
    _STUB_SCALARS.pop((pk, run_id), None)


def bulk_insert_mix_datapoints(
    project_dir: Path,
    run_id: str,
    datapoints: Iterable[tuple[str, float, float, dict[str, Any] | None]],
) -> int:
    lst = _STUB_DATAPOINTS.setdefault((_pkey(project_dir), run_id), [])
    added = 0
    for dp in datapoints:
        lst.append(dp)
        added += 1
    return added


def bulk_insert_mix_sections(
    project_dir: Path,
    run_id: str,
    sections: Iterable[tuple[float, float, str, str | None, float | None]],
) -> int:
    lst = _STUB_SECTIONS.setdefault((_pkey(project_dir), run_id), [])
    added = 0
    for sec in sections:
        lst.append(sec)
        added += 1
    return added


def set_mix_scalars(project_dir: Path, run_id: str, metrics: dict[str, float]) -> None:
    _STUB_SCALARS[(_pkey(project_dir), run_id)] = dict(metrics)


def get_mix_scalars(project_dir: Path, run_id: str) -> dict[str, float]:
    return dict(_STUB_SCALARS.get((_pkey(project_dir), run_id), {}))


def query_mix_sections(
    project_dir: Path,
    run_id: str,
    section_type: str | None = None,
) -> list[tuple[float, float, str, str | None, float | None]]:
    rows = list(_STUB_SECTIONS.get((_pkey(project_dir), run_id), []))
    if section_type is None:
        return rows
    return [r for r in rows if r[2] == section_type]
