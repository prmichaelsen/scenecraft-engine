# Spec: engine-server-bootstrap

> **Agent Directive**: This spec defines the exact observable behavior of `scenecraft server` process startup + initialization. Every scenario the system handles appears in the Behavior Table. Undecided scenarios are flagged `undefined` with a linked Open Question — never guessed into a test. Implementers translate each test in the Tests section into their framework of choice (pytest is the project norm).

---

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft

---

## Purpose

Define the black-box contract for the `scenecraft server` command's process startup + initialization sequence: CLI argument handling, work directory resolution, handler construction, HTTP socket binding, WebSocket thread spawning, plugin registration (in a fixed order), and signal-driven shutdown.

## Source

- `--from-draft` (implicit, via authored request) seeded with:
  - `acp.spec.md` (command directive)
  - `agent/reports/audit-2-architectural-deep-dive.md` §4 Engine Process Architecture + §1G Plugin Loading + Activation
- Primary code under test:
  - `src/scenecraft/cli.py` — `server()` Click command (lines 1284–1307)
  - `src/scenecraft/api_server.py` — `run_server()` (lines 10580–10642), `make_handler()` (lines 88–114), plugin activation block (lines 10605–10621)
  - `src/scenecraft/ws_server.py` — `start_ws_server()` (lines 415–425)
  - `src/scenecraft/config.py` — `resolve_work_dir()`, `set_projects_dir()`, `load_config()`

## Scope

### In scope

- The `scenecraft server --port <P> --host <H> --work-dir <D> --no-auth` invocation
- Work-directory resolution (CLI override → config → interactive prompt on first run)
- Construction of the HTTP handler via `make_handler(work_dir, no_auth)`
- HTTP server binding (`ThreadingMixIn` + `HTTPServer` on `(host, port)`)
- WebSocket thread spawn on `port + 1` via `start_ws_server`
- Plugin registration order: `isolate_vocals`, `transcribe`, `generate_music`, `light_show` (exactly this sequence)
- `--no-auth` effect on JWT gate construction
- Startup log output (banner: HTTP URL, WS URL, work dir, project count, plugin counts)
- `SIGINT` / `KeyboardInterrupt` → `server.shutdown()` path
- First-run projects directory prompting + persistence to `config.json`
- `FolderWatcher` sidecar singleton construction (non-activating)

### Out of scope

- Plugin lifecycle details beyond registration ordering → `local.engine-plugin-loading-lifecycle.md`
- CLI top-level entry (Click group, `main()`) → `local.engine-cli-admin-commands.md`
- REST dispatch semantics (routing, locking, error shapes) → `local.engine-rest-api-dispatcher.md`
- WS message protocol → separate chat/preview specs
- Config file schema beyond `projects_dir` key
- `interactive_console.start_if_tty()` behavior

## Requirements

1. **R1**: `scenecraft server` MUST default to `--host 0.0.0.0`, `--port 8890`, `--work-dir None`, `--no-auth False`.
2. **R2**: When `--work-dir` is provided, the command MUST resolve it verbatim (no config lookup) and create the directory if missing (`mkdir(parents=True, exist_ok=True)`).
3. **R3**: When `--work-dir` is NOT provided and `config.json` has `projects_dir`, the command MUST use that path without prompting.
4. **R4**: When `--work-dir` is NOT provided AND `config.json` has no `projects_dir`, the command MUST interactively prompt with default `~/.scenecraft/projects`, persist the chosen path via `set_projects_dir`, and then proceed.
5. **R5**: `run_server` MUST call `make_handler(work_dir, no_auth=no_auth)` before binding any socket.
6. **R6**: `run_server` MUST bind a `ThreadingMixIn`-mixed `HTTPServer` on `(host, port)` with `daemon_threads = True`.
7. **R7**: `run_server` MUST start the WebSocket server on `port + 1` in a background daemon thread BEFORE plugin registration.
8. **R8**: `run_server` MUST construct a module-level `FolderWatcher(work_dir)` and assign it to `scenecraft.ws_server.folder_watcher` before `start_ws_server` is called. The watcher MUST NOT activate any `inotify` watches at boot (watches are lazy, created on frontend request).
9. **R9**: `run_server` MUST register plugins via `PluginHost.register(...)` in exactly this order: (1) `isolate_vocals`, (2) `transcribe`, (3) `generate_music`, (4) `light_show`. No other first-party plugins are registered at boot.
10. **R10**: After registration, the server MUST log a single-line summary: `Plugins: N registered, M operations, K mcp tools`.
11. **R11**: The server MUST log HTTP URL, WS URL, resolved work dir, and project count before entering the serve loop.
12. **R12**: When `--no-auth` is `True`, `make_handler` MUST NOT call `find_root(work_dir)` and the resulting handler's `_authenticate()` MUST return `True` for every request.
13. **R13**: When `--no-auth` is `False` and `find_root(work_dir)` raises, the handler MUST still be constructed (exception swallowed) with `_sc_root = None`, behaving as if auth were disabled.
14. **R14**: `server.serve_forever()` MUST be called as the final boot step. `KeyboardInterrupt` (SIGINT) MUST trigger `server.shutdown()` and a single `"Shutting down."` log line.
15. **R15**: Plugin `deactivate` / `dispose` MUST NOT be invoked during shutdown (audit-2 leak #3: known gap; codified here to keep the spec honest).
16. **R16**: If `run_server` receives a `work_dir` string that does not resolve to an existing directory, it MUST print an error to stderr and exit with `SystemExit(1)` — BEFORE binding any socket.
17. **R17**: Plugin activation is invoked by `PluginHost.register(...)`. If any plugin's `activate()` raises, the exception currently propagates (audit-2 leak #2), aborting boot. Subsequent plugins in the order are NOT registered. The socket IS bound but NOT served (process exits before `serve_forever`).
18. **R18**: The handler class returned by `make_handler` MUST be a `BaseHTTPRequestHandler` subclass closed over `work_dir` and `no_auth`; a new instance is created per request by `ThreadingMixIn` (one thread per connection).

## Interfaces / Data Shapes

### CLI

```
scenecraft server
  [--port INT]        # default 8890
  [--host TEXT]       # default "0.0.0.0"
  [--work-dir TEXT]   # default None (falls back to config → prompt)
  [--no-auth]         # flag, default False
```

### `run_server(host, port, work_dir, no_auth)` signature

```python
def run_server(
    host: str = "0.0.0.0",
    port: int = 8890,
    work_dir: str | None = None,
    no_auth: bool = False,
) -> None  # never returns normally; SystemExit(1) on bad work_dir, blocks in serve_forever otherwise
```

### `make_handler(work_dir, no_auth)` signature

```python
def make_handler(
    work_dir: pathlib.Path,
    no_auth: bool = False,
) -> type[BaseHTTPRequestHandler]
```

Returned class exposes: `_authenticate()`, `do_GET`, `do_POST`, `do_OPTIONS`, `do_DELETE`, `do_PUT`, `log_message` (silenced). Closes over `_sc_root`, `_project_locks`, `_locks_lock`.

### `start_ws_server(host, port, work_dir)` signature

```python
def start_ws_server(
    host: str = "0.0.0.0",
    port: int = 8891,
    work_dir: Path | None = None,
) -> threading.Thread  # daemon thread running asyncio event loop
```

Side effect: sets module-level `_work_dir` in `ws_server.py`.

### config.json shape

```json
{
  "projects_dir": "/absolute/path/to/projects"
}
```

Location: `$XDG_CONFIG_HOME/scenecraft/config.json` (default `~/.config/scenecraft/config.json`). Legacy fallback: `~/.scenecraft/config.json` (auto-migrated on first load).

### Startup log contract (stderr)

```
[HH:MM:SS]   Plugins: 4 registered, <M> operations, <K> mcp tools
[HH:MM:SS] SceneCraft API server running at http://<host>:<port>
[HH:MM:SS] SceneCraft WebSocket server at ws://<host>:<port+1>
[HH:MM:SS]   Work dir: <resolved-absolute-path>
[HH:MM:SS]   Projects: <N>
[HH:MM:SS]
```

### Shutdown log contract

```
[HH:MM:SS] Shutting down.
```

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|---|---|---|
| 1 | `scenecraft server` with all defaults, config has `projects_dir` | HTTP binds 0.0.0.0:8890, WS on 8891, 4 plugins registered, auth enabled | `default-invocation-uses-config-projects-dir`, `ws-port-is-http-port-plus-one`, `plugin-registration-order-is-fixed` |
| 2 | `--work-dir /tmp/x` (existing) overrides config | Uses `/tmp/x` without prompting or reading config `projects_dir` | `cli-work-dir-overrides-config` |
| 3 | `--work-dir /tmp/new` (missing) | CLI creates dir via `mkdir(parents=True, exist_ok=True)` and proceeds | `cli-work-dir-creates-missing-directory` |
| 4 | First-run: no `--work-dir`, no `projects_dir` in config | Prompts user, persists choice to `config.json`, proceeds | `first-run-prompts-and-persists` |
| 5 | `--no-auth` passed | `_sc_root` is None; every `_authenticate()` call returns True | `no-auth-disables-jwt-gate` |
| 6 | Auth enabled but `find_root` raises | Exception swallowed; handler built with `_sc_root=None`; behaves auth-disabled | `find-root-failure-degrades-to-no-auth` |
| 7 | Port already in use | `OSError` (EADDRINUSE) propagates from `HTTPServer.__init__`; process exits non-zero; WS thread not started | `http-port-already-in-use-aborts-before-ws` |
| 8 | WS port (port+1) already in use | `undefined` — WS server runs inside a daemon thread; failure surfaces asynchronously | → [OQ-1](#open-questions) |
| 9 | `work_dir` string passed to `run_server` but path does not exist | Prints to stderr + `SystemExit(1)` before any socket bind | `nonexistent-work-dir-exits-before-bind` |
| 10 | `work_dir` exists but is not readable / writable | `undefined` — no permission check happens at boot; failures surface later per-request | → [OQ-2](#open-questions) |
| 11 | A plugin's `activate()` raises during `PluginHost.register(...)` | Exception propagates, aborts boot; later plugins not registered; `serve_forever` never called; HTTP socket bound but not serving (leaked until GC) | `plugin-activate-failure-aborts-boot` |
| 12 | `config.json` is corrupted (invalid JSON) | `undefined` — `load_config` will raise `json.JSONDecodeError` uncaught | → [OQ-3](#open-questions) |
| 13 | Legacy `~/.scenecraft/config.json` exists, new location does not | Auto-migrated to `$XDG_CONFIG_HOME/scenecraft/config.json` on first `load_config` | `legacy-config-auto-migrates` |
| 14 | `SIGINT` (Ctrl-C) during `serve_forever` | `server.shutdown()` called; `"Shutting down."` logged; process exits 0 | `sigint-triggers-shutdown` |
| 15 | `SIGTERM` during `serve_forever` | `undefined` — no explicit SIGTERM handler; default Python behavior kills the process (no `shutdown()` call, no plugin cleanup) | → [OQ-4](#open-questions) |
| 16 | `SIGKILL` | Process dies immediately; no cleanup possible (documented as expected) | `sigkill-leaves-no-cleanup-opportunity` |
| 17 | `--no-auth` used against a work_dir that has a `.scenecraft/` (production) root | `undefined` — currently silently permitted; no warning, no refusal | → [OQ-5](#open-questions) |
| 18 | Two `scenecraft server` processes on same `work_dir`, different ports | Both start successfully; they race on `sessions.db` writes (SQLite last-write-wins) | `concurrent-instances-same-workdir-race-sessions-db` |
| 19 | Two `scenecraft server` processes on same port | Second exits with EADDRINUSE (same as scenario 7) | `concurrent-instances-same-port-fails` |
| 20 | Plugin registration count logged post-registration | Log line reports 4 plugins, non-zero operation + mcp tool counts | `post-registration-summary-logged` |
| 21 | WS thread spawned before plugin registration | Boot order: `make_handler` → `HTTPServer` bind → WS thread → plugin register → banner → `serve_forever` | `boot-order-is-deterministic` |
| 22 | `FolderWatcher` singleton assigned before `start_ws_server` called | `ws_server.folder_watcher` is a `FolderWatcher` instance after boot, no `inotify` watches active | `folder-watcher-constructed-but-dormant` |
| 23 | Handler created per request (threaded) | Two concurrent requests execute on distinct threads; no shared handler instance state across requests | `threading-mixin-one-thread-per-request` |
| 24 | `run_server` invoked directly without going through Click `server()` | Behaves identically given equivalent arguments (no Click-only side effects in boot path) | `run-server-independent-of-click` |
| 25 | Plugin `deactivate`/`dispose` on shutdown | Not called; daemon threads + plugin-held file handles leak | `shutdown-does-not-deactivate-plugins` |

## Behavior (step-by-step)

`scenecraft server` boot sequence, in order:

1. **Click parses** flags; defaults applied: `--port 8890`, `--host 0.0.0.0`, `--work-dir None`, `--no-auth False`.
2. **Work-dir resolution** (`cli.py:1291–1304`):
   - Call `resolve_work_dir(work_dir)`.
   - If result is `None`: `click.prompt(...)` with default `~/.scenecraft/projects`; call `set_projects_dir(chosen)` (persists + `mkdir -p`); assign result to `wd`.
   - Else: `wd = Path(wd); wd.mkdir(parents=True, exist_ok=True)`.
3. **Hand off to `run_server`** (`cli.py:1306–1307`): `run_server(host, port, work_dir=str(wd), no_auth=no_auth)`.
4. **`run_server` re-resolves work_dir** (defensive): if string provided, `wd = Path(work_dir)`; else call `resolve_work_dir()`. If `wd is None or not wd.exists()`: stderr print + `SystemExit(1)`.
5. **Build handler**: `handler = make_handler(wd, no_auth=no_auth)` — constructs `_project_locks`, resolves `_sc_root` (unless `--no-auth`), returns `SceneCraftHandler` class.
6. **Define + instantiate HTTP server**: `ThreadedHTTPServer((host, port), handler)` with `daemon_threads = True`. Binds socket here; `EADDRINUSE` raises.
7. **Build folder watcher**: `_ws_mod.folder_watcher = FolderWatcher(wd)` (initializes inotify fd but adds no watches).
8. **Spawn WS thread**: `start_ws_server(host, port+1, work_dir=wd)` — daemon thread, asyncio event loop, `websockets.serve` on `(host, port+1)`.
9. **Register plugins** in fixed order: `isolate_vocals`, `transcribe`, `generate_music`, `light_show`. Each `PluginHost.register(mod)` loads the manifest, calls `mod.activate(plugin_api, context)`, and stores the result. Exceptions are NOT caught (leak #2).
10. **Log registration summary**: `Plugins: 4 registered, <M> operations, <K> mcp tools`.
11. **Log banner**: HTTP URL, WS URL, work dir, project count, blank line.
12. **Start interactive console** if stdin is a TTY (`interactive_console.start_if_tty()`) — out of scope, non-blocking.
13. **Enter `serve_forever`**: blocks; each HTTP request is dispatched on a new daemon thread.
14. **`KeyboardInterrupt`** (SIGINT) caught: `_log("Shutting down.")`, `server.shutdown()`. Plugin `dispose`/`deactivate` NOT called. WS thread + FolderWatcher thread die with the process (daemon).

## Acceptance Criteria

- [ ] `scenecraft server` with defaults binds 0.0.0.0:8890 + 0.0.0.0:8891 and registers exactly 4 plugins in the specified order.
- [ ] `--work-dir` provided + existing skips config lookup and prompt.
- [ ] `--work-dir` provided + missing creates the directory and proceeds.
- [ ] First-run with no config + no flag prompts once, persists, and proceeds.
- [ ] `--no-auth` bypasses JWT on every request.
- [ ] Port conflict on the HTTP port causes boot to fail before WS thread + plugin registration.
- [ ] `SystemExit(1)` on nonexistent `work_dir` happens before any socket bind.
- [ ] A failing plugin `activate()` aborts boot before `serve_forever` (current leak behavior; spec'd so the fix is a deliberate change).
- [ ] SIGINT triggers `server.shutdown()` and logs `"Shutting down."`; nothing else is called.
- [ ] Legacy `~/.scenecraft/config.json` is auto-migrated.
- [ ] All undefined rows in the Behavior Table map to live Open Questions.

## Tests

### Base Cases

The core boot contract: happy-path startup, argument handling, plugin order, and basic shutdown. A reader should understand normal boot from this subsection alone.

#### Test: default-invocation-uses-config-projects-dir (covers R1, R3, R5, R6, R7, R9)

**Given**:
- `config.json` contains `{"projects_dir": "/tmp/sc-test-<uuid>"}` (directory exists, empty)
- No `--work-dir` passed
- `scenecraft server` is invoked with all other defaults

**When**: boot proceeds to the point where `serve_forever` is about to be called (intercepted)

**Then** (assertions):
- **http-bind-host-port**: `HTTPServer.server_address == ("0.0.0.0", 8890)`
- **ws-thread-alive**: a daemon thread with target `_run_ws_server` is alive on port 8891
- **handler-is-subclass**: returned handler class is a subclass of `BaseHTTPRequestHandler`
- **no-prompt-issued**: `click.prompt` was never called
- **plugins-registered-count**: `PluginHost._registered` contains exactly 4 entries
- **plugin-order**: the registration call order observed is `isolate_vocals, transcribe, generate_music, light_show`

#### Test: cli-work-dir-overrides-config (covers R2)

**Given**:
- `config.json` has `projects_dir = /tmp/config-path`
- `--work-dir /tmp/cli-path` is passed (directory exists)

**When**: the `server()` Click command runs up to `run_server` invocation

**Then**:
- **wd-is-cli**: the `wd` forwarded to `run_server` resolves to `/tmp/cli-path`
- **no-config-read**: `load_config` was not consulted for `projects_dir` in the CLI branch
- **no-prompt-issued**: `click.prompt` was never called

#### Test: cli-work-dir-creates-missing-directory (covers R2)

**Given**: `--work-dir /tmp/sc-fresh-<uuid>` referring to a path that does not exist

**When**: `server()` runs

**Then**:
- **dir-created**: the path exists and `is_dir()` is True after the call
- **no-systemexit**: no `SystemExit` is raised during this step

#### Test: first-run-prompts-and-persists (covers R4)

**Given**:
- `config.json` absent (and legacy path absent)
- No `--work-dir` passed
- `click.prompt` patched to return `/tmp/sc-prompted-<uuid>`

**When**: `server()` runs through work-dir resolution

**Then**:
- **prompt-called-once**: `click.prompt` was invoked exactly one time
- **config-written**: `config.json` now contains `projects_dir = /tmp/sc-prompted-<uuid>` (absolute, resolved)
- **dir-created**: `/tmp/sc-prompted-<uuid>` exists
- **proceeds-to-run-server**: `run_server` is called afterward with the chosen path

#### Test: no-auth-disables-jwt-gate (covers R12)

**Given**: `make_handler(work_dir, no_auth=True)` is called with any work_dir

**When**: a handler instance is invoked with arbitrary `self.path` and headers

**Then**:
- **sc-root-none**: the closed-over `_sc_root` is `None`
- **authenticate-returns-true**: `_authenticate()` returns True with no `Authorization` header present
- **no-find-root-call**: `scenecraft.vcs.bootstrap.find_root` was NOT called during `make_handler`

#### Test: plugin-registration-order-is-fixed (covers R9)

**Given**: `run_server` runs with `PluginHost.register` patched to record the module argument passed in each call

**When**: boot completes plugin registration

**Then**:
- **order-exact**: recorded list equals `[isolate_vocals, transcribe, generate_music, light_show]` module-identity-wise
- **no-extras**: no other modules were registered in this block

#### Test: ws-port-is-http-port-plus-one (covers R7)

**Given**: `scenecraft server --port 9999`

**When**: boot reaches WS thread spawn

**Then**:
- **ws-port-is-10000**: `start_ws_server` was called with `port=10000`

#### Test: post-registration-summary-logged (covers R10)

**Given**: a successful boot through plugin registration

**When**: the banner-logging step runs

**Then**:
- **log-contains-count**: stderr contains a line matching the regex `Plugins: 4 registered, \d+ operations, \d+ mcp tools`

#### Test: boot-order-is-deterministic (covers R5, R6, R7, R9, R11)

**Given**: `run_server` patched to record the order of key calls: `make_handler`, `ThreadedHTTPServer.__init__`, `start_ws_server`, `PluginHost.register(isolate_vocals)`, `_log("SceneCraft API server running ...")`, `server.serve_forever`

**When**: boot runs to `serve_forever` (intercepted)

**Then**:
- **order**: recorded sequence is exactly `[make_handler, HTTPServer.__init__, start_ws_server, register×4, banner_log, serve_forever]`

#### Test: sigint-triggers-shutdown (covers R14, R15)

**Given**: `run_server` running with `server.serve_forever` patched to raise `KeyboardInterrupt`

**When**: the except branch runs

**Then**:
- **shutdown-called**: `server.shutdown()` was invoked exactly once
- **log-emitted**: stderr contains the line `"Shutting down."`
- **no-plugin-deactivate**: `PluginHost.deactivate` / `PluginHost.dispose_all` were NOT called

#### Test: shutdown-does-not-deactivate-plugins (covers R15, R25)

**Given**: bootstrapped server, SIGINT delivered

**When**: process handles the `KeyboardInterrupt`

**Then**:
- **no-dispose-call**: no `dispose*`, `deactivate*`, or shutdown hook on any registered plugin is invoked
- **daemon-threads-remain-until-exit**: plugin-spawned threads are not joined

### Edge Cases

Boundaries, concurrency, failure modes, and un-decided behaviors.

#### Test: http-port-already-in-use-aborts-before-ws (covers R6, R7)

**Given**: a socket is already bound to `0.0.0.0:8890`

**When**: `run_server(port=8890)` attempts to bind

**Then**:
- **oserror-raised**: `HTTPServer.__init__` raises `OSError` with errno `EADDRINUSE` (or equivalent platform code)
- **ws-not-started**: `start_ws_server` was NOT called
- **plugins-not-registered**: `PluginHost.register` was NOT called for any plugin
- **no-partial-serve**: `serve_forever` was NOT reached

#### Test: nonexistent-work-dir-exits-before-bind (covers R16)

**Given**: `run_server(work_dir="/definitely/does/not/exist")`

**When**: the function runs

**Then**:
- **systemexit-1**: raises `SystemExit` with code `1`
- **stderr-mentions-path**: stderr contains the substring `Work directory not found: /definitely/does/not/exist`
- **no-handler-built**: `make_handler` was NOT called
- **no-bind**: `HTTPServer.__init__` was NOT called

#### Test: plugin-activate-failure-aborts-boot (covers R17)

**Given**: `transcribe.activate` patched to raise `RuntimeError("boom")`

**When**: `run_server` runs

**Then**:
- **exception-propagates**: `RuntimeError("boom")` propagates out of `run_server`
- **later-plugins-not-registered**: `generate_music` and `light_show` are NOT in `PluginHost._registered`
- **earlier-plugins-registered**: `isolate_vocals` IS in `PluginHost._registered`
- **no-serve-forever**: `server.serve_forever` was NOT called
- **socket-was-bound**: `ThreadedHTTPServer` instance was created before the failure (leaked to GC)

#### Test: find-root-failure-degrades-to-no-auth (covers R13)

**Given**: `scenecraft.vcs.bootstrap.find_root` patched to raise `OSError`

**When**: `make_handler(work_dir, no_auth=False)` is called

**Then**:
- **handler-returned**: a handler class is returned (no exception escapes)
- **sc-root-none**: the closed-over `_sc_root` is `None`
- **authenticate-returns-true**: `_authenticate()` returns True with no Authorization header

#### Test: legacy-config-auto-migrates (covers R4)

**Given**:
- `~/.scenecraft/config.json` exists with `{"projects_dir": "/tmp/legacy"}` (directory exists)
- `$XDG_CONFIG_HOME/scenecraft/config.json` does NOT exist

**When**: `server()` runs with no `--work-dir`

**Then**:
- **new-file-written**: `$XDG_CONFIG_HOME/scenecraft/config.json` now exists with the same content
- **no-prompt-issued**: `click.prompt` was not called
- **proceeds-with-legacy-value**: `run_server` was called with `work_dir=/tmp/legacy`

#### Test: run-server-independent-of-click (covers R5, R6, R7, R9)

**Given**: `run_server("127.0.0.1", 9001, work_dir="/tmp/direct", no_auth=True)` is invoked directly (no Click)

**When**: boot runs with `serve_forever` intercepted

**Then**:
- **bound-to-127**: HTTP server address is `("127.0.0.1", 9001)`
- **ws-on-9002**: `start_ws_server` called with `port=9002`
- **four-plugins-registered**: plugin count is 4 in fixed order

#### Test: threading-mixin-one-thread-per-request (covers R6, R18)

**Given**: a running server (post-boot) with two concurrent slow GET requests to any endpoint

**When**: both requests are in flight simultaneously

**Then**:
- **two-threads-used**: the two requests execute on distinct `threading.Thread` instances
- **no-request-queued-on-first**: the second request's first byte of processing begins before the first request completes

#### Test: folder-watcher-constructed-but-dormant (covers R8)

**Given**: `run_server` runs to completion of the WS-thread-spawn step (intercepted)

**When**: the watcher assignment has occurred

**Then**:
- **watcher-is-foldewatcher**: `scenecraft.ws_server.folder_watcher` is an instance of `FolderWatcher`
- **no-watches-active**: `folder_watcher._wd_map` is empty AND `folder_watcher._running` is False (no inotify thread spawned yet)

#### Test: concurrent-instances-same-workdir-race-sessions-db (covers — see OQ-6)

**Given**: two `scenecraft server` processes started on the same `work_dir` but different ports (e.g., 8890 and 8892)

**When**: both complete boot and begin serving

**Then**:
- **both-start**: both processes reach `serve_forever` (no coordination / lockfile at boot)
- **no-boot-time-warning**: neither process logs a warning about a concurrent instance
- **sessions-db-shared-race**: writes to `sessions.db` from either process interleave with SQLite last-write-wins semantics (no cross-process lock above the SQLite level)

#### Test: concurrent-instances-same-port-fails (covers R6)

**Given**: one `scenecraft server --port 8890` is already running

**When**: a second `scenecraft server --port 8890` is started

**Then**:
- **second-fails-eaddrinuse**: second process exits with `OSError` (EADDRINUSE) during `HTTPServer.__init__`
- **first-unaffected**: first process continues to serve

#### Test: sigkill-leaves-no-cleanup-opportunity (covers R14 — negative)

**Given**: a running server

**When**: the process receives `SIGKILL`

**Then**:
- **no-shutdown-log**: no `"Shutting down."` log line is emitted
- **no-shutdown-called**: `server.shutdown()` is not called (unobservable from inside the killed process; verify indirectly by absence of graceful cleanup side effects on disk — e.g., no final flush)
- **sockets-released-by-os**: OS reclaims the TCP ports after TIME_WAIT (assertion: a fresh boot on the same port succeeds within a bounded retry window)

## Non-Goals

- Detecting port conflicts with a friendly error message (currently raw `OSError`; UX improvement deferred)
- Cross-process locking on `work_dir` to prevent concurrent `scenecraft server` on the same directory
- Graceful plugin shutdown (known gap — tracked by audit-2 leak #3 and the plugin-lifecycle spec)
- SIGTERM handling (currently undefined; see OQ-4)
- Production-mode safety interlocks on `--no-auth` (see OQ-5)
- `config.json` schema validation and corruption recovery (see OQ-3)
- Work-dir permission preflight (see OQ-2)
- WS-port conflict recovery (see OQ-1)

## Open Questions

- **OQ-1**: WS port (port+1) already in use. The WS server runs in a daemon thread calling `asyncio.run(_run_ws_server(...))`. If `websockets.serve` fails to bind, the exception is swallowed inside the daemon thread; the HTTP server continues running as if everything is fine. Should the boot sequence detect this and fail loudly? Currently undefined.
- **OQ-2**: `work_dir` exists but is not readable / writable by the running user. No preflight check. Should boot `stat` the directory and fail fast, or continue and let per-request handlers surface `PermissionError` when they hit disk?
- **OQ-3**: `config.json` is corrupted (invalid JSON). `load_config` calls `json.load` with no try/except. Should corruption trigger a prompt to re-initialize? Abort with a clear message? Silently overwrite?
- **OQ-4**: SIGTERM handling. Python's default SIGTERM behavior terminates the process without raising `KeyboardInterrupt`, so `server.shutdown()` is not called. Should the server install a `signal.signal(SIGTERM, ...)` handler that mirrors the SIGINT path?
- **OQ-5**: `--no-auth` in production (a work_dir that has a `.scenecraft/` root). Currently permitted silently. Should boot refuse? Emit a warning? Require `--no-auth-unsafe-i-know-what-im-doing`?
- **OQ-6**: Multiple concurrent `scenecraft server` instances on the same `work_dir`. The `sessions.db` (and per-project `project.db` files) are plain SQLite — WAL mode tolerates multi-reader/single-writer, but there is no boot-time coordination. Should boot acquire an advisory lock (e.g., `flock` on `<work_dir>/.scenecraft.lock`) and refuse to start if held? The cost of ignoring: occasional duplicate-insert races on `sessions.db` during login flows.

## Related Artifacts

- Audit: `agent/reports/audit-2-architectural-deep-dive.md` §4, §1G
- Related spec: `local.engine-plugin-loading-lifecycle.md` (plugin activation internals, LIFO dispose)
- Related spec: `local.engine-cli-admin-commands.md` (Click entrypoint, `vcs/cli.py` command group)
- Related spec: `local.engine-rest-api-dispatcher.md` (what `make_handler` returns, how routes dispatch)
- Related leaks: audit-2 #2 (plugin activate uncaught), #3 (no shutdown hook), #5 (CORS), #7 (per-project locks)

---

**Namespace**: local
**Spec**: engine-server-bootstrap
**Version**: 1.0.0
**Created**: 2026-04-27
**Status**: Draft — awaiting proof-read of Behavior Table and resolution of OQ-1 through OQ-6.
