"""Per-project lock registry (M16 T65).

Relocated from ``api_server.py`` during the hard cutover so the
FastAPI app can import the lock without pulling in the entire legacy
server module. The registry is module-level (process-global) so all
threads share the same per-project serialization state.
"""

from __future__ import annotations

import threading

_project_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_project_lock(project_name: str) -> threading.Lock:
    """Get (or create) a per-project lock for serializing YAML and git operations."""
    with _locks_lock:
        if project_name not in _project_locks:
            _project_locks[project_name] = threading.Lock()
        return _project_locks[project_name]


__all__ = ["_get_project_lock", "_project_locks"]
