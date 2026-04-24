"""Structural-route registry (M16 T59).

``STRUCTURAL_ROUTES`` is the opt-in set of route-tail names whose
handlers mutate the project timeline in a read-modify-write pattern
and therefore must (a) serialize under the per-project lock and (b)
trigger the timeline validator as a post-handler hook.

The frozenset mirrors ``_structural_routes`` inside
``api_server.py::do_POST`` verbatim — keep them in sync until T65
deletes the legacy handler.

Routers landing in T61 / T63 will import ``STRUCTURAL_ROUTES`` when
deciding whether to attach ``Depends(project_lock)`` to their POSTs.
The set is authoritative; the dependency itself gates validator
invocation on ``_route_is_structural(request)`` so a router that
forgets to exclude a non-structural sibling route still gets the
right behavior.
"""

from __future__ import annotations

from fastapi import Request


STRUCTURAL_ROUTES: frozenset[str] = frozenset(
    {
        "add-keyframe",
        "duplicate-keyframe",
        "delete-keyframe",
        "batch-delete-keyframes",
        "restore-keyframe",
        "delete-transition",
        "restore-transition",
        "split-transition",
        "insert-pool-item",
        "paste-group",
        "checkpoint",
    }
)


def _route_name(request: Request) -> str:
    """Tail segment of the request path (``…/add-keyframe`` → ``add-keyframe``).

    Matches the legacy ``path.rsplit('/', 1)[-1]`` computation in
    ``api_server.py::do_POST``. For the test harness
    (``/api/test-harness/{name}/structural-a``) this yields
    ``structural-a`` / ``structural-b`` so the harness routes can
    opt into the structural set without polluting the legacy set.
    """
    path = request.url.path
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[-1]


# Tests mount two extra structural names on top of the production
# set so the dependency exercises the full code path. Keeping these
# names in the module (rather than only in test code) lets the
# dependency's structural-check logic be a pure function of
# ``request.url.path`` — no "is this a test?" branching.
_TESTING_STRUCTURAL_NAMES: frozenset[str] = frozenset({"structural-a", "structural-b"})


def _route_is_structural(request: Request) -> bool:
    name = _route_name(request)
    return name in STRUCTURAL_ROUTES or name in _TESTING_STRUCTURAL_NAMES


__all__ = [
    "STRUCTURAL_ROUTES",
    "_route_name",
    "_route_is_structural",
]
