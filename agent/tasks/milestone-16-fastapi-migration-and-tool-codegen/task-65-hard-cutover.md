# Task 65: Hard cutover — CLI swap, delete api_server.py, perf baseline, suite-green

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.fastapi-migration`](../../specs/local.fastapi-migration.md) — R1, R3, R33–R35, R38, R39, R44
**Estimated Time**: 4–6 hours
**Dependencies**: T57, T58, T59, T60, T61, T62, T63, T64
**Status**: Not Started

---

## Objective

Pull the plug on `api_server.py`. Swap the CLI entry to launch uvicorn, migrate the 11 test files that instantiate the legacy server, delete `api_server.py`, and prove (a) the full 897-test suite is green, (b) no performance regression on the two hot paths, (c) WS server still runs independently, and (d) graceful shutdown during streaming works. This task ends the migration.

---

## TDD Plan

Before deletion, capture a **performance baseline** by running the `/render-frame` and `/files/*` Range load tests against the legacy server. Commit those numbers to `tests/fixtures/perf_baseline.json`. Write the perf-comparison tests that read this baseline and compare against the running FastAPI app. They will pass once the perf infrastructure works. Then perform the CLI swap and deletion, and run the full suite — any red test is a migration defect.

---

## Steps

### 1. Capture performance baseline (BEFORE deletion)

Write `scripts/capture_perf_baseline.py`:
- For `/render-frame?t=<t>&quality=80` at 5 representative `(project, t)` tuples: issue 100 sequential requests against the legacy server, record timings, compute p50 and p99.
- For `/api/projects/{name}/files/<large-media-file>` with `Range: bytes=<i*1M>-<(i+1)*1M>`: issue 100 range fetches across a 100 MB file, record MB/s throughput.
- Save to `tests/fixtures/perf_baseline.json`:
  ```json
  {
    "render_frame": {
      "P1_3.5_80": {"p50_ms": 65, "p99_ms": 180, "n": 100},
      ...
    },
    "files_range": {
      "p1_100mb.mp4": {"mb_per_sec": 2300.0, "n": 100}
    },
    "captured_at": "2026-04-24T...",
    "server": "api_server.py (legacy)"
  }
  ```

### 2. Migrate test fixtures from `HTTPServer` to `TestClient`

Identify (via T57's call-site audit) every test file that constructs the legacy server. Expected candidates (from the T57 scan): `tests/test_api.py`, any `test_audio_*` test that uses `make_handler`, etc.

For each:
- Replace:
  ```python
  from http.server import HTTPServer
  from scenecraft.api_server import make_handler
  server = HTTPServer(('', 0), make_handler(work_dir))
  # background thread...
  ```
  with:
  ```python
  from fastapi.testclient import TestClient
  from scenecraft.api.app import create_app
  client = TestClient(create_app(work_dir))
  ```
- Replace `urlopen(...)` calls with `client.get(...)` / `client.post(...)`.
- Test assertions (status, body, headers) should not need changes — they're checking the response contract, which is preserved.

### 3. CLI entry swap

Edit `src/scenecraft/cli.py`:
- Find the current server-start path (imports `scenecraft.api_server`, calls `HTTPServer(...).serve_forever()`).
- Replace with:
  ```python
  import uvicorn
  from scenecraft.api.app import create_app
  app = create_app(work_dir)
  uvicorn.run(app, host=host, port=port, log_level=...)
  ```
- Preserve all existing CLI flags (`--host`, `--port`, `--work-dir`, etc.) with identical defaults.
- WS server launch is unchanged (still `start_ws_server(...)` on its own thread/port).

### 4. Relocate `_get_project_lock`

Move `_get_project_lock` and the `_project_locks` registry from `api_server.py` to a new module `src/scenecraft/locks.py`. Update T59's import path (`deps.py::project_lock`) to `from scenecraft.locks import _get_project_lock`.

### 5. Delete `api_server.py`

```bash
git rm src/scenecraft/api_server.py
```

Run `git grep "from scenecraft.api_server\|import api_server"`. Expected: **zero results** in `src/` and `tests/`. Any straggler must be updated to use `scenecraft.api.app` or `scenecraft.locks`.

### 6. Remove T59 test-harness routes

The `/api/test-harness/structural-a` + `/structural-b` routes from T59 can be deleted from the production app now — real structural routes from T61 prove the `project_lock` dependency works. Keep the tests that use them by migrating to hit real routes (`add-keyframe`, etc.).

### 7. Tests to Pass

Create / update `tests/test_cutover.py` and a few spot-check tests; the big signal is the **entire existing suite** now runs against FastAPI.

- `cli_starts_uvicorn` — `subprocess.Popen(["scenecraft", "serve", "--port", "0"])`; connect to the bound port; `GET /openapi.json` returns 200; kill the process.
- `cli_help_unchanged` — run `scenecraft --help` and compare output to a pre-migration fixture (exact string match, or tolerant of version-header differences only).
- `legacy_server_deleted` — assert `not (src_path / "scenecraft" / "api_server.py").exists()`; grep `src/` and `tests/` for `api_server` imports; expect zero.
- `legacy_test_suite_green` — meta-test or CI job: `pytest tests/` exits 0 with the same test count as pre-migration baseline.
- `render_frame_perf_no_regression` — load `tests/fixtures/perf_baseline.json`; run 100 `/render-frame` calls against the FastAPI app; assert `new_p50 ≤ 1.10 × legacy_p50` and `new_p99 ≤ 1.25 × legacy_p99` per fixture tuple.
- `files_range_perf_no_regression` — same; assert `new_mb_per_sec ≥ 0.90 × legacy_mb_per_sec`.
- `test_client_replaces_http_server_fixture` — meta-test that grep's test files for `HTTPServer(.*make_handler` imports; expect zero.
- `ws_server_independent` — start FastAPI on 8890, start WS on 8891, verify both serve; WS broadcast reaches a connected client while a FastAPI request is in-flight.
- `graceful_shutdown_during_stream` — start a long `/download-preview` stream; issue SIGTERM to uvicorn; assert client sees EOF within 30 s, no ERROR-level traceback in log.
- `chat_exec_paths_unaffected` — import `scenecraft.chat` and call `_exec_add_audio_track(project_dir, input_data)` directly (bypassing HTTP); assert it works and no socket opened (`socket.socket` monkey-patched to assert never called).

---

## Verification

- [ ] `api_server.py` does NOT exist in the repo
- [ ] `git grep "from scenecraft.api_server\|import api_server"` in `src/` and `tests/` returns zero results
- [ ] `scenecraft --help` output matches pre-migration fixture
- [ ] Full `pytest tests/` passes with zero failures and zero errors
- [ ] Test count matches pre-migration baseline
- [ ] `/render-frame` p50 within 10% of legacy; p99 within 25%
- [ ] `/files/*` Range throughput within 10% of legacy
- [ ] WS server on 8891 unaffected
- [ ] Graceful shutdown during streaming: no traceback; clean EOF
- [ ] Chat `_exec_*` paths unaffected (zero HTTP opens)

---

## Tests Covered

`cli-starts-uvicorn`, `cli-help-unchanged`, `legacy-server-deleted`, `legacy-test-suite-green`, `render-frame-perf-no-regression`, `files-range-perf-no-regression`, `test-client-replaces-http-server-fixture`, `ws-server-independent`, `graceful-shutdown-during-stream`, `chat-exec-paths-unaffected`.

---

## Notes

- **This task is the point of no return.** After merging, rolling back means reverting the whole milestone's commits. The 897-test suite is the confidence anchor.
- The perf tests are deliberately loose (10% p50, 25% p99). FastAPI + uvicorn usually matches or beats stdlib `http.server` on modern Python; tighter bars can be added in a follow-up once a steady-state baseline is established.
- `test_client_replaces_http_server_fixture` is partly a meta-check. If any pre-migration test still uses `HTTPServer`, either it was missed in T57's audit (fix it now) or it's testing something orthogonal to the API (keep it, and exclude from the grep).
- If `scenecraft --help` text differs meaningfully from pre-migration output (e.g., argparse descriptions changed), update the fixture — don't mask the diff.
- After cutover, open a tracking issue for "evaluate chat tool execution path" (codegen OQ-1). Not blocking M16.
