# Task 59: Structural lock dependency + timeline validator post-hook

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.fastapi-migration`](../../specs/local.fastapi-migration.md) — R18, R19, R40, R45, R53
**Estimated Time**: 3–4 hours
**Dependencies**: T57, T58
**Status**: Not Started

---

## Objective

Port the per-project structural-mutation lock + post-mutation timeline validator from `api_server.py::do_POST` (lines 710–750) into a FastAPI dependency. Prove under test that:

1. Concurrent structural mutations on the same project serialize.
2. Concurrent mutations on **different** projects do NOT serialize.
3. Timeline validator warnings broadcast over WS after success.
4. Validator exceptions don't fail the request.
5. The lock is released even when the handler raises.

This is the infrastructure that T61's keyframes/transitions routers rely on.

---

## TDD Plan

Create two throwaway structural routes for testing: `POST /api/test-harness/{name}/structural-a` and `.../structural-b`. Both use the `project_lock` dependency. The handlers call into a test double that touches a shared counter or sleeps briefly so concurrency is observable. Write the six concurrency/exception tests. They fail (no dependency exists). Implement `project_lock` and the post-mutation validator middleware until all six pass. Delete the test-harness routes at the end of the task (or keep them gated behind a pytest-only flag).

---

## Steps

### 1. `deps.py::project_lock`

```python
from scenecraft.api_server import _get_project_lock  # re-use existing lock registry
# (After T65 deletes api_server.py, the lock registry moves to scenecraft.locks or similar.
#  For now, keep the import path compatible.)

async def project_lock(name: str):
    lock = _get_project_lock(name)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
```

Apply to structural routes via `dependencies=[Depends(project_lock)]`.

### 2. Structural-route tagging

Define a module-level set for documentation + future audits:

```python
# src/scenecraft/api/structural.py
STRUCTURAL_ROUTES = frozenset({
    "add-keyframe", "duplicate-keyframe", "delete-keyframe",
    "batch-delete-keyframes", "restore-keyframe",
    "delete-transition", "restore-transition",
    "split-transition", "insert-pool-item", "paste-group",
    "checkpoint",
})
```

Routers in T61, T63 will reference this set when applying the dependency.

### 3. Timeline validator middleware

The validator must run **inside** the lock scope (so the next structural request doesn't start before validation finishes). Two shapes are acceptable:

**Option A — wrap in the dependency:**

```python
async def project_lock_with_validation(name: str, request: Request):
    lock = _get_project_lock(name)
    lock.acquire()
    try:
        yield
        # post-handler: run validator
        _run_timeline_validator(request, name)
    except Exception:
        lock.release()  # only release on error here; success path releases in finally below
        raise
    else:
        lock.release()
```

**Option B — add a post-route callback via `Depends` with yield:**

```python
async def project_lock(name: str, request: Request):
    lock = _get_project_lock(name)
    lock.acquire()
    try:
        yield
    finally:
        # Post-handler block
        if _route_is_structural(request):
            try:
                _run_timeline_validator(request, name)
            except Exception as ve:
                _log(f"  Validation error: {ve}")
        lock.release()
```

Prefer **B** (simpler, one function). `_route_is_structural(request)` inspects `request.scope["route"].path` and checks against `STRUCTURAL_ROUTES`.

### 4. `_run_timeline_validator(request, project_name)`

```python
def _run_timeline_validator(request, project_name: str) -> None:
    from scenecraft.db import validate_timeline
    work_dir = request.app.state.work_dir
    project_dir = work_dir / project_name
    if not (project_dir / "project.db").exists():
        return
    warnings = validate_timeline(project_dir)
    if not warnings:
        return
    _log(f"⚠ Timeline validation ({_route_name(request)}): {len(warnings)} issues")
    for w in warnings[:10]:
        _log(f"  - {w}")
    # Broadcast via WS — match legacy shape exactly
    try:
        from scenecraft.ws_server import job_manager as _jm
        _jm._broadcast({
            "type": "timeline_warning",
            "route": _route_name(request),
            "warnings": warnings,
        })
    except Exception:
        pass
```

### 5. Test harness routes

Add to `tests/conftest.py` or a test-only router included when `app.state.testing` is True:

```python
@router.post("/api/test-harness/{name}/structural-a", dependencies=[Depends(project_lock)])
async def _test_structural_a(name: str, body: dict): ...

@router.post("/api/test-harness/{name}/structural-b", dependencies=[Depends(project_lock)])
async def _test_structural_b(name: str, body: dict): ...
```

Each handler records entry/exit timestamps into a shared list so tests can observe ordering.

### 6. Tests to Pass

Create `tests/test_fastapi_structural_lock.py`:

- `structural_lock_serializes` — two concurrent POSTs to `structural-a` on `P1` via `asyncio.gather`; assert handler-entry timestamps don't overlap (second starts ≥ 5 ms after first completes).
- `structural_lock_is_per_project` — concurrent POSTs on `P1` and `P2`; assert entry timestamps **do** overlap (second starts before first completes).
- `timeline_validator_runs_after_mutation` — monkey-patch `validate_timeline` to return `["warn1"]`; monkey-patch `job_manager._broadcast` to record invocations; issue one structural POST; assert a `timeline_warning` broadcast was recorded with the right shape.
- `validator_exception_non_fatal` — monkey-patch validator to raise `ValueError("boom")`; issue structural POST; assert response is 200, log contains `Validation error: boom`, no 500.
- `lock_released_on_exception` — monkey-patch handler to raise on first call; issue first POST (expect 500), then second POST (expect 200 within 100 ms — lock must have been released).
- `validator_exception_lock_released` — monkey-patch validator to raise; two sequential structural POSTs; second completes within 100 ms of first (lock released despite validator raising).

### 7. Cleanup

Once tests pass, gate the test-harness routes behind `if app.state.testing:`. Real structural routes land in T61 and T63 — they will use the same `project_lock` dependency.

---

## Verification

- [ ] All 6 named tests pass
- [ ] `structural_lock_serializes` demonstrates actual serialization via timestamp inspection (not just "both succeed")
- [ ] `structural_lock_is_per_project` confirms cross-project parallelism
- [ ] Timeline validator runs inside lock scope (verified by scheduling a second structural request during validation and observing it blocks)
- [ ] Validator exceptions logged but never raised
- [ ] Lock released on handler exception AND validator exception
- [ ] WS broadcast shape exactly matches legacy: `{"type": "timeline_warning", "route": <route_name>, "warnings": [...]}`

---

## Tests Covered

`structural-lock-serializes`, `structural-lock-is-per-project`, `timeline-validator-runs-after-mutation`, `validator-exception-non-fatal`, `lock-released-on-exception`, `validator-exception-lock-released`.

---

## Notes

- `_get_project_lock` currently lives in `api_server.py`. Don't move it yet — T65 will relocate it to `scenecraft.locks` along with deleting `api_server.py`. Import the same symbol here to avoid a circular refactor.
- `_route_name(request)` — extract the last path segment for routes like `/api/projects/{name}/add-keyframe` → `"add-keyframe"` to match legacy's `path.rsplit("/", 1)[-1]` shape used in the `_structural_routes` check.
- The test-harness routes are debt. Plan to delete them in T65 after real structural routes prove the dependency works end-to-end.
