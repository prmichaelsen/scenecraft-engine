"""Structural-route registry (M16 T59, T65 cutover).

``STRUCTURAL_ROUTES`` is the opt-in set of route-tail names whose
handlers mutate the project timeline in a read-modify-write pattern
and therefore must (a) serialize under the per-project lock and (b)
trigger the timeline validator as a post-handler hook.

The dependency gates validator invocation on
``_route_is_structural(request)`` so a router that forgets to exclude
a non-structural sibling route still gets the right behavior.
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
        # M16 T61 — net-new REST surface for a previously chat-only op.
        # Legacy has no HTTP route for this, so there's no legacy set to
        # keep in sync with. T65 will delete the legacy set anyway.
        "batch-delete-transitions",
    }
)


def _route_name(request: Request) -> str:
    """Tail segment of the request path (``…/add-keyframe`` → ``add-keyframe``)."""
    path = request.url.path
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[-1]


def _route_is_structural(request: Request) -> bool:
    name = _route_name(request)
    return name in STRUCTURAL_ROUTES


__all__ = [
    "STRUCTURAL_ROUTES",
    "_route_name",
    "_route_is_structural",
]
