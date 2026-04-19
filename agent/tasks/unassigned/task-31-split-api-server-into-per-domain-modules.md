# Task 31: Split api_server.py into per-domain API modules

**Milestone**: Unassigned
**Design Reference**: None (straight refactor; no new behavior)
**Estimated Time**: 2-3 days
**Dependencies**: None
**Status**: Not Started

---

## Objective

Break up `src/scenecraft/api_server.py` (7417 lines) into a directory of per-domain modules under `src/scenecraft/api/` plus a central `api/index.py` router that dispatches to them. No behavior change; the HTTP surface is identical before and after.

---

## Context

`src/scenecraft/api_server.py` has grown to 7417 lines containing ~70 HTTP routes across roughly 15 distinct domains (keyframes, transitions, tracks, audio tracks/clips, pool, workspace views, checkpoints, chat, generation, media/thumb, settings, markers, undo, oauth, config). The file is:

- Hard to navigate (keyword search is the only way to find a handler)
- Hostile to diff review (unrelated changes collide)
- Easy to forget a handler exists (e.g. we've stubbed audio-intelligence twice)
- Slow to open in editors (cold index, IntelliSense churn)

This refactor establishes a conventional per-domain module layout. Each module owns a cohesive set of routes plus their helpers. A single `index.py` handles routing; each module exposes pure `do_<method>(handler, path, match) -> bool` functions that the router calls in order.

---

## Target Layout

```
src/scenecraft/api/
├── __init__.py
├── index.py              # Router: method + regex dispatch table; imports all modules
├── common.py             # Shared helpers: _json_response, _error, _cors_headers, _read_json_body,
│                         #   _require_project_dir, _get_project_dir, project lock helpers, auth check
├── auth.py               # /auth/login, /auth/logout
├── config.py             # GET/POST /api/config
├── oauth.py              # /api/oauth/*
├── projects.py           # /api/projects (list, create), /api/browse
├── keyframes.py          # /api/projects/:name/keyframes, add-keyframe, delete-keyframe,
│                         #   duplicate-keyframe, paste-group, batch-delete-keyframes,
│                         #   restore-keyframe, update-timestamp, update-prompt,
│                         #   set-base-image, batch-set-base-image, unlink-keyframe,
│                         #   select-keyframes, select-slot-keyframes
├── transitions.py        # delete-transition, restore-transition, update-transition-action,
│                         #   update-transition-remap, generate-transition-action,
│                         #   enhance-transition-action, select-transitions
├── tracks.py             # /api/projects/:name/tracks (+ audio-tracks, audio-clips)
├── pool.py               # /api/projects/:name/pool/* (list, add, upload, import, rename,
│                         #   tag, untag, gc, gc-preview, tags, assign-pool-video, duplicate)
├── workspace.py          # workspace-views + checkpoints (both panel-layout-related)
├── chat.py               # chat endpoints
├── generation.py         # generate-keyframe-candidates, generate-transition-candidates,
│                         #   generate-keyframe-variations, generate-audio-*, etc.
├── media.py              # /files/*, /thumb/*, /thumbnail/*, video-candidates,
│                         #   unselected-candidates, download-preview, staging/*
├── settings.py           # settings, section-settings, effects, preview-quality
├── narrative.py          # narrative, timelines, sections, markers, ingredients,
│                         #   bench, prompt-roster, descriptions
├── undo.py               # undo, redo, undo-history
└── watched_folders.py    # watched-folders, watch-folder, unwatch-folder
```

`src/scenecraft/api_server.py` shrinks to a thin shim that:
1. Creates the `BaseHTTPRequestHandler` subclass
2. In `do_GET`/`do_POST`/`do_DELETE`, delegates to `api.index.dispatch(self, method, path)`
3. Starts the HTTP server (unchanged)

The existing `run_server()` signature and CLI wiring stay the same.

---

## Steps

### 1. Create the `api/` package scaffolding

```
src/scenecraft/api/__init__.py    # empty
src/scenecraft/api/common.py      # shared helpers (copy + adapt)
src/scenecraft/api/index.py       # router
```

The router's shape:

```python
# src/scenecraft/api/index.py
from __future__ import annotations

import re
from typing import Callable
from http.server import BaseHTTPRequestHandler

from . import auth, config, oauth, projects, keyframes, transitions, tracks
from . import pool, workspace, chat, generation, media, settings as settings_api
from . import narrative as narrative_api, undo, watched_folders

# (method, regex, handler) — first match wins
_ROUTES: list[tuple[str, re.Pattern, Callable]] = []

def _register(method: str, pattern: str, fn: Callable):
    _ROUTES.append((method, re.compile(pattern), fn))

# Each module exposes a register(register_fn) function that calls _register for its routes.
# This keeps the route list next to the handlers it refers to.
for mod in (auth, config, oauth, projects, keyframes, transitions, tracks, pool,
            workspace, chat, generation, media, settings_api, narrative_api, undo,
            watched_folders):
    mod.register(_register)

def dispatch(handler: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    """Route a request. Returns True if handled, False if no route matched."""
    for m, pat, fn in _ROUTES:
        if m != method:
            continue
        match = pat.match(path)
        if match:
            fn(handler, match)
            return True
    return False
```

Each module exports a `register(r)` function:

```python
# src/scenecraft/api/keyframes.py
def register(r):
    r("GET",  r"^/api/projects/([^/]+)/keyframes$",           _get_keyframes)
    r("POST", r"^/api/projects/([^/]+)/add-keyframe$",        _add_keyframe)
    r("POST", r"^/api/projects/([^/]+)/delete-keyframe$",     _delete_keyframe)
    # ... etc

def _get_keyframes(handler, match):
    project_name = match.group(1)
    ...
```

### 2. Extract `common.py`

Move the following from the current handler class into module-level functions that take `handler` as first argument:

- `_json_response(handler, data, status=200)`
- `_error(handler, status, code, message)`
- `_cors_headers(handler)`
- `_read_json_body(handler)` (returns dict or None; None signals the helper already sent 400)
- `_require_project_dir(handler, project_name, work_dir)` → Path | None
- `_get_project_dir(handler, project_name, work_dir)` → Path | None
- Project lock helpers (`_get_project_lock`, `_yaml_lock`)
- Auth: `_authenticated_user` accessor, token validation call-site
- `_log(msg)` — stays at module level in both places; exported from common

The handler class keeps thin wrappers that delegate to these (so any code still referencing `self._json_response(...)` during the migration keeps working).

### 3. Move routes in order, one module at a time

Do NOT attempt to move everything in one PR. Order:

1. `common.py` — then `api_server.py` imports and uses helpers from here while all routes stay in the handler. Verify tests still pass.
2. `auth.py` — smallest surface (`/auth/login`, `/auth/logout`). Verify login flow still works end-to-end in a browser.
3. `config.py` — two routes. Trivial.
4. `oauth.py` — 3-4 routes.
5. `projects.py` — list, create, browse. Medium.
6. `tracks.py` — video tracks + audio tracks + audio clips. The new audio routes live here from day one.
7. `keyframes.py`
8. `transitions.py`
9. `pool.py` — biggest domain after keyframes/transitions.
10. `workspace.py` — workspace views + checkpoints.
11. `chat.py`
12. `generation.py` — the async job endpoints (use WebSocket for progress).
13. `media.py` — files, thumb, thumbnail, Range requests.
14. `settings.py`, `narrative.py`, `undo.py`, `watched_folders.py` — remaining smaller domains.

After each module lands, `api_server.py` loses its corresponding routes and gains a delegation to `dispatch()`. The old routes are deleted only after the new ones are proven working.

### 4. Replace the main dispatch in `api_server.py`

After all modules are moved, `api_server.py` `do_GET`/`do_POST`/`do_DELETE` reduce to:

```python
def do_GET(self):
    from urllib.parse import urlparse, unquote
    parsed = urlparse(self.path)
    path = unquote(parsed.path)
    if not api.index.dispatch(self, "GET", path):
        self._error(404, "NOT_FOUND", f"No route: GET {path}")

def do_POST(self):
    ...  # same shape

def do_DELETE(self):
    ...  # same shape
```

The handler class retains:
- `__init__` plumbing
- The `_authenticated_user` field (populated before dispatch)
- CORS preflight (`do_OPTIONS`)
- BaseHTTPRequestHandler overrides (`log_message` silencing, etc.)

Target line count for `api_server.py` after refactor: **under 300 lines**.

### 5. Update imports in callers

- `src/scenecraft/cli.py` — `from scenecraft.api_server import run_server` stays the same (run_server is unchanged).
- Any tests importing internal helpers (`_json_response`, etc.) update to `from scenecraft.api.common import ...`.

### 6. Regression verification

Manual smoke test after each module move:
- Browser login flow works (auth)
- Projects list loads (projects)
- Editor opens a project (keyframes, transitions, tracks, pool)
- Panel drag-resize saves (workspace-views)
- Media serves with Range requests (media)

Automated tests in `tests/test_api.py` should all continue to pass without changes — they hit the HTTP surface, not internal module structure.

---

## Verification

- [ ] `src/scenecraft/api/` package created with all 16 modules listed in Target Layout
- [ ] `src/scenecraft/api/index.py` contains the routing table populated by each module's `register()` function
- [ ] `src/scenecraft/api/common.py` owns all shared helpers; no duplication across modules
- [ ] Each domain module exports a `register(r)` function and handler functions, no class state
- [ ] `src/scenecraft/api_server.py` is under 300 lines
- [ ] Every route that existed before the refactor is dispatched correctly after
- [ ] `tests/test_api.py` passes with zero modifications
- [ ] Browser login flow works end-to-end on the running server
- [ ] Panel layout save (`/workspace-views/_autosave_v3`) succeeds from the editor
- [ ] Media endpoints (`/files/*`, `/thumb/*`) honor Range headers
- [ ] Audio tracks/clips CRUD endpoints work (added recently — carried over intact)
- [ ] `grep -rn "def _json_response" src/` shows exactly one definition
- [ ] No file in `src/scenecraft/api/` exceeds 600 lines (guideline, not a hard rule)

---

## Expected Output

### File Structure

```
src/scenecraft/
├── api/                          # NEW
│   ├── __init__.py
│   ├── index.py                  # router
│   ├── common.py                 # helpers
│   ├── auth.py
│   ├── config.py
│   ├── oauth.py
│   ├── projects.py
│   ├── keyframes.py
│   ├── transitions.py
│   ├── tracks.py
│   ├── pool.py
│   ├── workspace.py
│   ├── chat.py
│   ├── generation.py
│   ├── media.py
│   ├── settings.py
│   ├── narrative.py
│   ├── undo.py
│   └── watched_folders.py
└── api_server.py                 # SLIMMED — handler class + run_server, delegates to api.index
```

### Key Files

- `api/index.py`: routing table + `dispatch(handler, method, path) -> bool`
- `api/common.py`: shared helpers (JSON response, CORS, auth, project resolution, locks)
- `api/<domain>.py`: one per domain; exports `register(r)` + private handler functions
- `api_server.py`: under 300 lines; creates the handler class, delegates routing, runs the server

### Line Count Delta

- Before: `api_server.py` = 7417 lines
- After: `api_server.py` ≈ 250 lines; `api/` total ≈ 7200 lines spread across 18 files; average ~400 lines per module

---

## Common Issues and Solutions

### Issue 1: Circular import
**Symptom**: `ImportError: cannot import name X from partially initialized module`
**Solution**: `api/common.py` must not import any domain module. Domain modules import from `common`, not each other. If two modules need to share logic, factor it into `common.py` or a new helper module.

### Issue 2: `handler.work_dir` or closure state not available
**Symptom**: Handler functions can't access `work_dir` captured in the current `make_handler` closure.
**Solution**: Promote `work_dir` to a handler attribute in `api_server.py` (`self.work_dir = work_dir` in __init__, or class attribute via `make_handler` factory). Handler functions take `handler` as first arg and read `handler.work_dir`.

### Issue 3: Route ordering bug
**Symptom**: A specific route stops working because a more general pattern matches first.
**Solution**: Routes register in module-import order. Make patterns specific (anchor with `$`). If conflicts arise, the router could sort by pattern length descending, but it's cleaner to write non-overlapping patterns.

### Issue 4: Test failure in `tests/test_api.py`
**Symptom**: A test that was passing breaks after moving its target route.
**Solution**: The refactor is supposed to be behavior-preserving. Bisect the move — compare the handler logic pre and post. Common causes: accidentally stripped an authentication check, changed a default parameter, or dropped a log line that a test asserts on.

---

## Notes

- This is a pure refactor. Ship before any new feature work that touches these endpoints (including the backend-rendered preview streaming work in `local.backend-rendered-preview-streaming`). The preview streaming design assumes a new `api/render.py` module, which only exists after this task.
- No behavior change. If you notice a bug during the move, fix it in a separate commit with a clear message — don't bundle bug fixes into the refactor.
- Keep commits small and testable: one per domain module. Makes `git revert` easy if a specific module regresses.
- The `register(r)` convention is a simple pattern-based router. If we ever add type-safe routing (Flask/FastAPI-style decorators), this refactor puts us in a good position to adopt that, but it's not in scope here.

---

**Next Task**: (none currently)
**Related Design Docs**:
- [local.backend-rendered-preview-streaming.md](../../design/local.backend-rendered-preview-streaming.md) — downstream work that depends on the refactor (the new `api/render.py` module lives in this layout).
