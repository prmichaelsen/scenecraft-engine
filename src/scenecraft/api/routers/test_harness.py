"""Test-harness router for M16 T59 structural-lock concurrency tests.

Mounted **only** when ``create_app(testing=True)`` sets
``app.state.testing = True``. Never reachable from the production
``uvicorn scenecraft.api.app:app`` boot because the module-level
``app`` is built with ``testing=False``.

Two POST routes (``structural-a`` / ``structural-b``) each depend on
``project_lock`` so they serialize exactly the way real structural
routes will in T61/T63. Each handler:

  1. Records an ``enter`` event in ``_HARNESS_LOG`` with a monotonic
     timestamp and the project name.
  2. Invokes an optional hook from ``_HANDLER_HOOKS`` — tests inject
     a sleep or a raise here to shape concurrency and error behavior.
  3. Records an ``exit`` event.

Timestamps use ``time.monotonic()`` (not wall time) so assertions
about overlap/serialization are immune to system-clock jitter.
``_HARNESS_LOG`` is a plain list guarded by a ``threading.Lock`` —
FastAPI may dispatch handlers on the event loop thread OR on the
starlette-run-in-threadpool worker pool, so a lock is mandatory.

This router is debt — T65's hard cutover will delete the module
outright once real structural routes prove the dependency
end-to-end.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from fastapi import APIRouter, Depends

from scenecraft.api.deps import project_lock


router = APIRouter(tags=["_test_harness"])


# Shared state — reset by the test fixture between cases.
_HARNESS_LOG: list[dict[str, Any]] = []
_HARNESS_LOG_LOCK = threading.Lock()

# Per-route-name hook. Tests assign callables that either sleep to
# create measurable overlap or raise to test the error paths.
_HANDLER_HOOKS: dict[str, Callable[[], None]] = {}


def _record(project: str, route: str, phase: str) -> None:
    with _HARNESS_LOG_LOCK:
        _HARNESS_LOG.append(
            {
                "project": project,
                "route": route,
                "phase": phase,
                "ts": time.monotonic(),
            }
        )


def _run_hook(route: str) -> None:
    hook = _HANDLER_HOOKS.get(route)
    if hook is not None:
        hook()


# Handlers are deliberately sync (``def``, not ``async def``) so FastAPI
# offloads them to the starlette threadpool. That lets concurrent requests
# actually run in parallel — an ``async def`` handler with a blocking
# ``time.sleep`` inside the injected hook would stall the event loop and
# make the "per-project lock lets different projects overlap" test racy.
@router.post(
    "/api/test-harness/{name}/structural-a",
    operation_id="_test_harness_structural_a",
    dependencies=[Depends(project_lock)],
    include_in_schema=False,
)
def _harness_structural_a(name: str, body: dict | None = None) -> dict:
    _record(name, "structural-a", "enter")
    try:
        _run_hook("structural-a")
    finally:
        _record(name, "structural-a", "exit")
    return {"ok": True, "name": name}


@router.post(
    "/api/test-harness/{name}/structural-b",
    operation_id="_test_harness_structural_b",
    dependencies=[Depends(project_lock)],
    include_in_schema=False,
)
def _harness_structural_b(name: str, body: dict | None = None) -> dict:
    _record(name, "structural-b", "enter")
    try:
        _run_hook("structural-b")
    finally:
        _record(name, "structural-b", "exit")
    return {"ok": True, "name": name}


__all__ = [
    "router",
    "_HARNESS_LOG",
    "_HANDLER_HOOKS",
]
