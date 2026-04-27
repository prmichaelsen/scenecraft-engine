# Spec: Engine Plugin Loading + Activation Lifecycle

> **🤖 Agent Directive**: This is an implementation-ready spec describing the
> as-built behavior of the scenecraft-engine plugin boot / activation /
> shutdown lifecycle, plus the deltas between that as-built behavior and the
> scenecraft frontend spec (`local.plugin-host-and-manifest.md`) which assumes
> a more mature host.

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Active

---

**Purpose**: Define the black-box boot sequence, plugin discovery, manifest
loading, activation, contribution registration, disposal, and shutdown behavior
of `PluginHost` in scenecraft-engine.

**Source**: `--from-draft` (audit-2 §1G + leaks #2, #3, #18 +
`plugin_host.py` + `api_server.py:run_server` + `mcp_server.py` + `cli.py`).

---

## Scope

### In scope
- Process boot sequence for `scenecraft server` (api_server) and
  `python -m scenecraft.mcp_server` (mcp_server stdio satellite).
- Plugin **discovery**: hardcoded Python imports. No filesystem scan, no
  entry-points, no `plugin_dir` env var.
- Plugin **registration order** (load-order-dependent behavior).
- `PluginHost.register(module)` lifecycle: manifest load → activate()
  call → context storage.
- Manifest load-failure policy (non-fatal, log-and-continue).
- Activation failure policy (uncaught exceptions propagate out of
  `run_server` and crash the process).
- `PluginHost.deactivate(name)` Disposable LIFO semantics.
- Shutdown: what happens (and what does not) on SIGINT / `server.shutdown()`.
- The absence of a `register_migration` API — including the contract that
  plugins MUST own their schema via `plugin_api` helpers (`create_table`,
  `add_column`) inside `activate()`, not via a migration registry.
- Divergence from `scenecraft/agent/specs/local.plugin-host-and-manifest.md`.

### Out of scope
- Manifest schema, `plugin.yaml` field-level validation
  (→ `scenecraft/agent/specs/local.plugin-host-and-manifest.md`).
- Individual plugins' internal behavior (isolate_vocals, transcribe,
  generate_music, light_show, generate_foley).
- `plugin_api` surface (record_spend, broadcast_event, etc. —
  → `scenecraft/agent/specs/local.plugin-api-surface-and-r9a.md`).
- `register_declared` internals for operations/mcpTools/restEndpoints (covered
  by the scenecraft plugin-host spec at the interface level; this spec only
  constrains the call from `register()`).
- Dynamic / hot plugin reload (not possible today, flagged as `undefined`).
- Dependency ordering between plugins (not currently modeled; flagged as
  `undefined`).

---

## Requirements

1. **R1 (hardcoded discovery)**: Plugin discovery MUST be by hardcoded
   `from scenecraft.plugins import <name>` statements followed by
   `PluginHost.register(<module>)` calls. No filesystem scan, no entry
   points, no manifest-based discovery.
2. **R2 (api_server registration order)**: In `api_server.run_server`, the
   hardcoded import + register sequence MUST be, in order:
   `isolate_vocals`, `transcribe`, `generate_music`, `light_show`.
3. **R3 (mcp_server mirrors api_server)**: `mcp_server.py` module-level code
   MUST register the same plugin set in the same order as R2, and the module
   docstring explicitly calls this out as the contract.
4. **R4 (generate_foley not registered)**: The `generate_foley` plugin module
   exists under `src/scenecraft/plugins/generate_foley/` with a `plugin.yaml`
   but is NOT registered by either api_server or mcp_server as of this spec.
   Its registration is **undefined** (→ OQ-5).
5. **R5 (manifest load is non-fatal)**: `PluginHost.register` MUST attempt
   `load_manifest(module)`. Any exception raised by `load_manifest` MUST be
   caught, logged to stderr with the module name and exception type/message,
   and registration MUST proceed with `manifest = None`. Manifest-load failure
   alone MUST NOT abort boot.
6. **R6 (manifest populated pre-activate)**: When manifest load succeeds,
   `context.manifest` MUST be set BEFORE the plugin's `activate()` is called,
   and the manifest MUST be stored in `PluginHost._manifests[manifest.name]`.
7. **R7 (activate signature adaptation)**: If the plugin exports
   `activate`, the host MUST inspect the callable's signature and call
   `activate(plugin_api, context)` if it accepts ≥2 parameters, otherwise
   `activate(plugin_api)`.
8. **R8 (activate exceptions propagate — fatal)**: If a plugin's
   `activate()` raises, the exception MUST propagate out of
   `PluginHost.register`, out of `run_server`, and terminate the engine
   process. No try/except wraps activation. No atomic rollback of previously
   registered contributions from the failing plugin occurs.
   **This is the audit-2 leak #2 behavior and contradicts the scenecraft
   frontend spec R31 (atomic activation).** (→ OQ-2)
9. **R9 (double-register is idempotent)**: Calling `PluginHost.register` on
   a module already present in `_contexts` MUST return the existing context
   without re-running manifest load or `activate()`.
10. **R10 (registration order recorded)**: After successful registration,
    the module name MUST be appended to `_registered` (used for diagnostics).
11. **R11 (Disposable LIFO on deactivate)**: `PluginHost.deactivate(name)`
    MUST pop each item from `context.subscriptions` and call `.dispose()` on
    it in LIFO order (last-registered disposes first). A `dispose()` that
    raises MUST be caught, logged with the plugin name, and the next
    disposable MUST still be disposed.
12. **R12 (optional plugin-level deactivate hook)**: After all subscriptions
    are disposed, if the plugin module exports a callable `deactivate`, it
    MUST be invoked as `deactivate(context)`. Exceptions from the plugin-level
    hook MUST be caught and logged; they MUST NOT propagate. `ModuleNotFoundError`
    from re-importing the module MUST be swallowed silently.
13. **R13 (deactivate on unknown plugin is no-op)**: Calling
    `deactivate(name)` for a name not in `_contexts` MUST be a silent no-op.
14. **R14 (no shutdown hook)**: The engine's server shutdown path
    (`server.shutdown()` on KeyboardInterrupt) MUST NOT call
    `deactivate(name)` for any plugin today. No `deactivate_all` exists.
    Daemon threads, file watchers, and open file handles registered as
    Disposables WILL leak on process shutdown.
    **This is audit-2 leak #3.** (→ OQ-3)
15. **R15 (no register_migration API)**: `PluginHost` MUST NOT expose a
    `register_migration` method. Plugin sidecar tables are created by
    `plugin_api.create_table` / `add_column` inside the plugin's
    `activate()` body (or at first-use lazily, per plugin). There is no
    version table, no migration registry, no ordering guarantee across
    plugins.
    **This is audit-2 leak #18 and contradicts the scenecraft
    frontend spec which describes a `register_migration` surface.** (→ OQ-4)
16. **R16 (no discovery alternative path)**: `api_server.run_server` and
    `mcp_server` module-level code are the ONLY two plugin activation paths.
    The scenecraft CLI `server` command (cli.py:1289) invokes
    `run_server` and inherits R2; it does NOT have its own independent
    activation path.
17. **R17 (mcp_server is a distinct process)**: The stdio MCP server runs
    in a SEPARATE OS process from `api_server`. Both processes independently
    register the same plugins. There is no shared `PluginHost` state across
    processes; `PluginHost` class-level state is per-process.
18. **R18 (no hot reload)**: There is no code path to reload a plugin in a
    running engine. Code changes require a full process restart.
    (→ OQ-6 for dev-mode hot reload.)
19. **R19 (log line on boot)**: After all plugins register in
    `run_server`, a single log line MUST be emitted summarizing:
    `Plugins: N registered, M operations, K mcp tools` (using the
    current `_registered`/`_operations`/`_mcp_tools` counts).
20. **R20 (plugin dependency order undefined)**: The host MUST NOT perform
    any dependency analysis between plugins. If plugin A's `activate()` reads
    a table plugin B creates, the fact that B is registered after A would
    manifest as an `activate()`-time error (propagating per R8). No explicit
    dependency declaration exists. (→ OQ-1)

---

## Interfaces / Data Shapes

### `PluginHost.register(plugin_module) -> PluginContext`
- **Input**: a Python module with optional `activate(plugin_api, context)`
  and optional `deactivate(context)` attributes, optionally shipping
  `plugin.yaml` alongside.
- **Side effects**:
  - Reads `plugin.yaml` via `load_manifest` (best-effort).
  - Mutates class-level `_manifests`, `_contexts`, `_registered`.
  - Calls into plugin code (`activate`) which itself mutates
    `_operations`, `_mcp_tools`, `_rest_routes_by_method` via
    `register_*` helpers.
- **Returns**: the fresh or existing `PluginContext`.
- **Raises**: anything the plugin's `activate()` raises (not wrapped).

### `PluginHost.deactivate(name: str) -> None`
- **Input**: the plugin module's `__name__`.
- **Side effects**:
  - Pops the context from `_contexts`, removes from `_registered`.
  - LIFO disposes every `context.subscriptions` entry.
  - Calls `module.deactivate(context)` if the plugin provides one.
- **Raises**: nothing (all exceptions caught internally).

### Boot sequence (api_server)
```
scenecraft server
 └─ cli.py:server() → run_server(host, port, work_dir, no_auth)
    ├─ resolve work_dir
    ├─ build HTTP handler + ThreadedHTTPServer (daemon_threads=True)
    ├─ start WS server on port+1
    ├─ import PluginHost
    ├─ import scenecraft.plugins.isolate_vocals
    ├─ import scenecraft.plugins.transcribe
    ├─ import scenecraft.plugins.generate_music
    ├─ import scenecraft.plugins.light_show
    ├─ PluginHost.register(isolate_vocals)   # raise → engine dies here
    ├─ PluginHost.register(transcribe)       # raise → engine dies here
    ├─ PluginHost.register(generate_music)   # raise → engine dies here
    ├─ PluginHost.register(light_show)       # raise → engine dies here
    ├─ _log("Plugins: N registered, …")
    ├─ start interactive console (if TTY)
    └─ server.serve_forever()
         ↓ KeyboardInterrupt
         └─ server.shutdown()   # ⚠ no deactivate_all
```

### Boot sequence (mcp_server)
```
python -m scenecraft.mcp_server
 ├─ module-level imports of the four plugin modules
 ├─ module-level PluginHost.register() calls (same order as api_server)
 └─ asyncio stdio server loop
```

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | `run_server` boots, all four plugins activate cleanly | Host registers four modules in order (isolate_vocals, transcribe, generate_music, light_show); boot-log line emitted; `serve_forever` runs | `happy-path-boot-registers-four-plugins-in-order`, `boot-log-emitted-with-counts` |
| 2 | Plugin module has no `plugin.yaml` | `load_manifest` raises; error logged to stderr; registration continues with `manifest=None` | `missing-manifest-logs-and-continues` |
| 3 | Plugin's `plugin.yaml` is malformed | `load_manifest` raises; error logged; `context.manifest` stays `None`; `activate()` still called | `malformed-manifest-non-fatal` |
| 4 | Plugin exports `activate(plugin_api, context)` | Called with both args; `context.manifest` populated first if manifest loaded | `activate-2arg-called-with-context` |
| 5 | Plugin exports `activate(plugin_api)` (1-arg) | Called with just `plugin_api`; context still created and stored | `activate-1arg-signature-adapted` |
| 6 | Plugin exports no `activate` at all | Registration still succeeds; context stored empty; no error | `no-activate-function-is-fine` |
| 7 | Plugin's `activate()` raises `RuntimeError` | Exception propagates out of `PluginHost.register` → out of `run_server` → engine process exits non-zero; later plugins in the list NEVER register | `activate-raises-crashes-engine`, `activate-failure-blocks-later-plugins` |
| 8 | `PluginHost.register(module)` called twice on same module | Second call returns the existing context; no re-activate; no duplicate `_registered` entry | `double-register-is-idempotent` |
| 9 | `deactivate(name)` on a registered plugin with 3 Disposables | Each disposes in LIFO order; `_contexts[name]` removed; `_registered` pruned | `deactivate-disposes-lifo` |
| 10 | One `Disposable.dispose()` raises mid-teardown | Exception caught + logged; remaining disposables still dispose | `dispose-error-does-not-halt-teardown` |
| 11 | Plugin exports a module-level `deactivate(context)` | Called AFTER all subscriptions disposed | `plugin-level-deactivate-runs-after-subscriptions` |
| 12 | Module-level `deactivate(context)` raises | Exception caught + logged; no propagation | `plugin-level-deactivate-error-is-swallowed` |
| 13 | `deactivate("nonexistent")` | Silent no-op; no log, no error | `deactivate-unknown-plugin-is-noop` |
| 14 | Engine receives SIGINT / Ctrl-C | `server.shutdown()` called; socket closes; **no plugin deactivation** runs; daemon threads orphaned | `shutdown-does-not-deactivate-plugins` |
| 15 | Plugin A (registered first) tries to read a table owned by plugin B (registered second) inside A's `activate()` | B's table does not yet exist → SQL error → A's `activate()` raises → engine crashes (per R8) | `cross-plugin-dependency-wrong-order-crashes` |
| 16 | Code looks up `PluginHost.register_migration(...)` | `AttributeError` — method does not exist; plugins must use `plugin_api.create_table` / `add_column` inside `activate()` instead | `no-register-migration-api` |
| 17 | Developer edits a plugin's code while engine is running | No hot reload; must restart the process to pick up changes | `no-hot-reload-requires-process-restart` |
| 18 | `mcp_server` process starts | Module-level code registers the same four plugins in the same order as api_server | `mcp-server-mirrors-api-server-order` |
| 19 | `api_server` and `mcp_server` both running | Two independent processes each with their own `PluginHost` class state; no shared registry | `plugin-host-is-per-process` |
| 20 | `generate_foley` plugin module exists but is not in the import list | Not registered; its ops/tools/routes are absent from `PluginHost.list_*` | `generate-foley-not-registered-today` |
| 21 | Plugin A depends on plugin B's sidecar table but activation order is A-before-B | `undefined` — today it would crash per R8+R20; no dependency DSL exists; authors must manually order imports | → [OQ-1](#open-questions) |
| 22 | Plugin `activate()` raises — is engine-death the INTENDED contract? | `undefined` — code does it, but it contradicts scenecraft frontend spec R31 (atomic activation). Engine must decide: match frontend spec (try/except) or document engine divergence permanently | → [OQ-2](#open-questions) |
| 23 | Shutdown hook to call `deactivate_all()` — worth adding or defer? | `undefined` — leak #3 HIGH severity but daemon threads are typically fine on process exit; decision pending | → [OQ-3](#open-questions) |
| 24 | `register_migration` API — add it or redesign plugin schema ownership? | `undefined` — absence contradicts scenecraft frontend spec (R18); need to decide whether the engine adds the API, or the scenecraft spec is revised down to the imperative `create_table`-inside-activate model | → [OQ-4](#open-questions) |
| 25 | Is `generate_foley` activation intended to land before or after some milestone? | `undefined` — module exists but isn't wired up; missing from the hardcoded import list in both api_server and mcp_server | → [OQ-5](#open-questions) |
| 26 | Dev-mode hot reload of a plugin without full process restart | `undefined` — not possible today; is it worth building, or will `restart` console command cover it? | → [OQ-6](#open-questions) |
| 27 | Two discovery paths (api_server vs mcp_server) — which is authoritative? | `undefined` — today they are kept in sync by hand; drift is likely. Should mcp_server import api_server's registration function instead of duplicating the list? | → [OQ-7](#open-questions) |

---

## Behavior

### Boot (api_server / `scenecraft server`)
1. `cli.py:server()` validates flags, resolves `work_dir`, calls
   `run_server(host, port, work_dir, no_auth)`.
2. `run_server` constructs the HTTP server and starts the WS server.
3. `run_server` imports `PluginHost`, then each plugin module in the
   hardcoded order (R2).
4. For each plugin `M`, calls `PluginHost.register(M)`:
   - If `M` already in `_contexts`: return existing context, stop.
   - Create fresh `PluginContext(name=M.__name__)`.
   - Try `load_manifest(M)`:
     - On success: store in `_manifests[manifest.name]` and on
       `context.manifest`.
     - On exception: print to stderr, keep `manifest = None`, continue.
   - If `M.activate` exists: introspect signature; call with 1 or 2 args.
     Any exception propagates (R8).
   - Store `_contexts[M.__name__] = context`; append to `_registered`.
5. After all four plugins register, emit the summary log line.
6. Start the interactive console if TTY.
7. `server.serve_forever()`.

### Boot (mcp_server)
- Module-level code duplicates steps 3–4 above. No `run_server` wrapper.
- If any plugin's `activate()` raises, the `python -m scenecraft.mcp_server`
  process exits before `stdio_server()` ever runs.

### Deactivation
- Only invoked by explicit callers (tests, `_reset_for_tests`, future
  `deactivate_all`). Not invoked by the engine on shutdown today (R14).
- Pops context; LIFO disposes; runs plugin-level `deactivate` hook if
  present.

### Shutdown
- `KeyboardInterrupt` bubbles to the `try` in `run_server`, which calls
  `server.shutdown()`. That closes the HTTP socket; the WS thread is a
  daemon and dies with the process; plugin-owned daemon threads / watchers
  that were registered as `Disposable`s are NOT disposed (R14).

---

## Acceptance Criteria

- [ ] Removing any of the four hardcoded imports in `api_server.run_server`
  causes the corresponding ops/mcp-tools/REST routes to be absent from the
  running engine.
- [ ] A deliberately-broken `activate()` raise in any plugin prevents
  `run_server` from reaching `serve_forever`.
- [ ] A deliberately-broken `plugin.yaml` in any plugin logs a stderr line
  but the engine still boots and the other plugins still register.
- [ ] After SIGINT, no plugin `Disposable` is disposed (verifiable by a
  test disposable that writes to stderr on dispose — the line never
  appears).
- [ ] `PluginHost.register_migration` does not exist (AttributeError on
  access).
- [ ] `mcp_server.py` top-level register calls list the exact same four
  modules in the same order as `api_server.run_server`.
- [ ] Calling `PluginHost.register(mod)` twice does not duplicate any
  entry in `_registered` or call `activate()` twice.
- [ ] `generate_foley` is not in `_registered` after boot.

---

## Tests

### Base Cases

#### Test: happy-path-boot-registers-four-plugins-in-order (covers R1, R2, R6, R7, R10)
**Given**: a fresh `PluginHost` state (`_reset_for_tests`) and the four
first-party plugin modules on the import path with working `activate`
functions.
**When**: `run_server`'s plugin-registration block executes.
**Then**:
- **registered-count-4**: `len(PluginHost._registered) == 4`.
- **order-matches**: `PluginHost._registered == ["scenecraft.plugins.isolate_vocals", "scenecraft.plugins.transcribe", "scenecraft.plugins.generate_music", "scenecraft.plugins.light_show"]`.
- **manifests-present**: `PluginHost.get_manifest("isolate_vocals")` etc. return non-None for plugins that ship `plugin.yaml`.
- **context-per-plugin**: each module's `PluginContext` has `name` and, for 2-arg activates, `manifest` populated before activate ran.

#### Test: boot-log-emitted-with-counts (covers R19)
**Given**: happy-path boot.
**When**: plugin registration completes.
**Then**:
- **log-line-contains-counts**: stdout/log contains `"Plugins: 4 registered"` and counts for operations and mcp tools that match `len(PluginHost._operations)` and `len(PluginHost._mcp_tools)`.

#### Test: missing-manifest-logs-and-continues (covers R5, R6)
**Given**: a plugin module `p_no_manifest` whose directory has no `plugin.yaml`.
**When**: `PluginHost.register(p_no_manifest)`.
**Then**:
- **stderr-logged**: stderr contains `"[plugin-host] manifest load failed for p_no_manifest"`.
- **context-manifest-none**: the returned `PluginContext.manifest is None`.
- **registered-ok**: `p_no_manifest.__name__ in PluginHost._registered`.
- **activate-still-called**: the plugin's `activate()` was invoked exactly once.

#### Test: malformed-manifest-non-fatal (covers R5)
**Given**: a plugin with a malformed `plugin.yaml` that makes `load_manifest` raise `PluginManifestError`.
**When**: `PluginHost.register(plugin)`.
**Then**:
- **stderr-contains-exception-type**: logged line includes `PluginManifestError`.
- **no-exception-propagated**: `register` returns normally.
- **context-manifest-none**: `context.manifest is None`.

#### Test: activate-2arg-called-with-context (covers R7)
**Given**: plugin module `p` with `def activate(plugin_api, context): context._called = True`.
**When**: `PluginHost.register(p)`.
**Then**:
- **called-with-context**: `p`'s context has `_called == True`.
- **manifest-available-during-activate**: if `p` ships a manifest, `context.manifest` was non-None when `activate` entered.

#### Test: activate-1arg-signature-adapted (covers R7)
**Given**: plugin module with `def activate(plugin_api): ...`.
**When**: `PluginHost.register(plugin)`.
**Then**:
- **called-with-one-arg**: activate ran without TypeError.
- **context-stored**: `PluginHost._contexts[plugin.__name__]` is present.

#### Test: no-activate-function-is-fine (covers R7)
**Given**: plugin module with no `activate` attribute.
**When**: `PluginHost.register(plugin)`.
**Then**:
- **no-error**: call returns normally.
- **context-exists**: stored with empty `subscriptions`.
- **in-registered**: name appears in `_registered`.

#### Test: activate-raises-crashes-engine (covers R8)
**Given**: plugin module with `def activate(plugin_api, context): raise RuntimeError("boom")`.
**When**: `PluginHost.register(plugin)`.
**Then**:
- **exception-propagates**: `RuntimeError("boom")` is raised to the caller.
- **not-in-registered**: the plugin's name is NOT in `_registered`.
- **context-not-stored**: the plugin's name is NOT a key in `_contexts`.
- **manifest-side-effect-present**: if `plugin.yaml` loaded successfully, `_manifests` DOES still contain the manifest (manifest-caching happens before activate is called, per plugin_host.py:213–215).

#### Test: activate-failure-blocks-later-plugins (covers R8)
**Given**: four plugin modules A, B, C, D; B's `activate` raises.
**When**: the `run_server` sequence calls `register(A)`, `register(B)`, `register(C)`, `register(D)` in order.
**Then**:
- **a-registered**: A is in `_registered`.
- **b-not-registered**: B is NOT in `_registered`.
- **c-and-d-never-attempted**: register was never called for C or D (i.e. `register(B)`'s exception aborted the sequence).

#### Test: double-register-is-idempotent (covers R9)
**Given**: plugin `p` already registered with a `Disposable` in its subscriptions.
**When**: `PluginHost.register(p)` is called a second time.
**Then**:
- **returns-existing-context**: the returned context is the same object as the first call.
- **registered-length-unchanged**: `_registered.count(p.__name__) == 1`.
- **activate-not-rerun**: `p.activate` was invoked exactly once total.

#### Test: deactivate-disposes-lifo (covers R11)
**Given**: plugin `p` registered with three disposables D1, D2, D3 appended in that order.
**When**: `PluginHost.deactivate(p.__name__)`.
**Then**:
- **order-d3-d2-d1**: `.dispose()` call order was D3, then D2, then D1.
- **context-removed**: `p.__name__ not in _contexts`.
- **registered-removed**: `p.__name__ not in _registered`.

#### Test: dispose-error-does-not-halt-teardown (covers R11)
**Given**: three disposables where D2.dispose raises `RuntimeError("d2-broken")`.
**When**: `deactivate(name)`.
**Then**:
- **all-three-attempted**: D1 and D3 dispose calls happened even though D2 raised.
- **stderr-logged**: stderr contains `"dispose failed for <name>"` and `"d2-broken"`.
- **no-exception-propagated**: `deactivate` returned normally.

#### Test: plugin-level-deactivate-runs-after-subscriptions (covers R12)
**Given**: plugin `p` with `def deactivate(context): context._deactivated = True` and two disposables.
**When**: `PluginHost.deactivate(p.__name__)`.
**Then**:
- **disposables-first**: the two disposables were disposed before `p.deactivate` ran.
- **hook-called**: the plugin's `deactivate` was called exactly once.
- **context-passed**: the argument received was the same `PluginContext` instance.

#### Test: plugin-level-deactivate-error-is-swallowed (covers R12)
**Given**: plugin `p` whose `deactivate(context)` raises.
**When**: `PluginHost.deactivate(p.__name__)`.
**Then**:
- **no-exception-propagated**: call returns normally.
- **stderr-logged**: stderr contains `"plugin deactivate() failed for"`.

#### Test: deactivate-unknown-plugin-is-noop (covers R13)
**Given**: empty `PluginHost` state.
**When**: `PluginHost.deactivate("does.not.exist")`.
**Then**:
- **no-exception**: call returns `None`.
- **no-log**: stderr is empty (no error line).

#### Test: no-register-migration-api (covers R15)
**Given**: the `PluginHost` class.
**When**: `getattr(PluginHost, "register_migration", None)`.
**Then**:
- **is-none**: the attribute does not exist on the class.
- **no-migration-table**: no `schema_migrations` table is created or maintained by the host.

#### Test: mcp-server-mirrors-api-server-order (covers R3, R16)
**Given**: source files `mcp_server.py` and `api_server.py`.
**When**: parsing module-level (or `run_server`-level) `PluginHost.register(...)` call sequences.
**Then**:
- **same-four-modules**: both files register exactly `isolate_vocals`, `transcribe`, `generate_music`, `light_show`.
- **same-order**: the order of calls is identical across files.

#### Test: generate-foley-not-registered-today (covers R4)
**Given**: a booted `run_server`.
**When**: inspecting `PluginHost._registered`.
**Then**:
- **not-present**: `"generate_foley"` (or its full module name) is not in `_registered`.
- **no-ops-or-tools**: no operation or mcp tool whose plugin id is `generate_foley` is in the respective registries.

### Edge Cases

#### Test: shutdown-does-not-deactivate-plugins (covers R14)
**Given**: `run_server` running with a plugin whose `activate` registered a test `Disposable` that writes `"D-DISPOSED"` to stderr on dispose.
**When**: process receives SIGINT, `server.shutdown()` runs, `run_server` returns.
**Then**:
- **no-dispose-line**: stderr does NOT contain `"D-DISPOSED"`.
- **no-deactivate-call**: `PluginHost._contexts` still contains the plugin's context after `run_server` returns (deactivate was never invoked).

#### Test: plugin-host-is-per-process (covers R17)
**Given**: both `scenecraft server` (api_server) and `python -m scenecraft.mcp_server` running as separate OS processes.
**When**: api_server calls `PluginHost.deactivate("isolate_vocals")` in its own process.
**Then**:
- **mcp-unaffected**: mcp_server's process still has `"isolate_vocals"` in `PluginHost._registered`.
- **api-affected**: api_server's process no longer has it.

#### Test: cross-plugin-dependency-wrong-order-crashes (covers R20)
**Given**: plugin A whose `activate` SELECTs from table `B__thing`; plugin B whose `activate` creates `B__thing`; registration order is A then B.
**When**: `PluginHost.register(A)`.
**Then**:
- **a-activate-raises**: SQL error ("no such table: B__thing") propagates.
- **b-never-registered**: register was never called for B.
- **engine-would-crash**: consistent with R8 (contract is intentional at the host level, even if unfortunate at the plugin-author level).

#### Test: no-hot-reload-requires-process-restart (covers R18)
**Given**: `run_server` running with plugin `p` containing op `p__do_x`.
**When**: `p.py` is edited on disk to change `p__do_x` behavior, without restarting the process.
**Then**:
- **old-behavior-retained**: subsequent calls to `p__do_x` run the previously-imported code, not the on-disk version.
- **no-reload-api**: no `PluginHost.reload(name)` or equivalent method exists.

#### Test: single-threaded-activation (covers R2, R8)
**Given**: `run_server` plugin-registration block.
**When**: concurrent threads attempt to call `PluginHost.register` for different modules.
**Then**:
- **not-contract**: this spec does NOT guarantee concurrent registration safety. The engine's contract is that plugin registration is performed sequentially on the boot thread only; any concurrent caller is outside the supported surface.
- **serialized-at-boot**: `run_server` always calls `register` on a single thread in sequence.

---

## Non-Goals

- No filesystem-scan plugin discovery.
- No entry-points / importlib.metadata discovery.
- No atomic rollback of a plugin's own partially-registered contributions
  on `activate()` failure in this spec. (The `register_declared` helper
  has its OWN partial-failure semantics per the scenecraft frontend spec
  R31; those semantics govern one method call within `activate()`, not
  the engine-level boot sequence. Engine-level boot is all-or-nothing per
  plugin module: activate succeeds → plugin is up; activate fails →
  engine exits.)
- No `register_migration` API in this spec.
- No shutdown hook / `deactivate_all` in this spec.
- No hot reload / plugin sandbox / plugin isolation.
- No dependency graph between plugins.
- No thread-safety guarantee for `register` / `deactivate` called
  concurrently.

---

## Open Questions

- **OQ-1 (dependency ordering)**: Plugin A depends on plugin B's table,
  but the hardcoded import list puts A before B. Today this surfaces as
  an engine crash (R8+R20). Options: (a) accept — authors must order
  imports correctly; (b) add a declarative `dependsOn: [b]` manifest
  field and topologically sort; (c) defer all schema creation to
  first-call inside the plugin, so activate order becomes irrelevant.
  **Decision needed before any plugin actually has cross-plugin
  dependencies.** Related: audit-2 §3 leak #8.

- **OQ-2 (activate exceptions — intended contract?)**: The engine code
  today lets `activate()` exceptions propagate and crash boot (R8). The
  scenecraft frontend spec `local.plugin-host-and-manifest.md` R31
  defines *atomic activation* — catch, LIFO-dispose partial state, raise
  a structured `PluginActivationError`. These two specs currently
  **contradict** each other. Is engine-death the intended contract, or
  should the engine align with the frontend spec? Audit-2 §6
  recommendation #1 says "align to the spec."

- **OQ-3 (shutdown hook worth adding?)**: Audit-2 leak #3 — daemon
  threads leak on restart. Is this worth fixing (add `deactivate_all`
  called on SIGINT, bounded by a timeout), or is it fine to rely on
  process-exit to reap daemon threads? Note: the `restart` interactive
  console command already executes a fresh interpreter; it does NOT
  currently call `deactivate_all` either.

- **OQ-4 (register_migration API)**: Audit-2 leak #18 — scenecraft
  frontend spec describes a `register_migration` surface; the engine
  does not have one. Options: (a) add it, with a `schema_migrations`
  version table (audit-2 §6 recommendation #4); (b) amend the scenecraft
  frontend spec down to the current imperative `create_table`-in-activate
  model; (c) ship a thin compatibility shim. Pick one.

- **OQ-5 (generate_foley activation)**: The module exists under
  `src/scenecraft/plugins/generate_foley/` with a `plugin.yaml`, but
  neither `api_server.run_server` nor `mcp_server.py` imports or
  registers it. Is this intentional (milestone-gated) or an oversight?

- **OQ-6 (hot reload in dev)**: Is it worth adding a dev-mode
  `PluginHost.reload(name)` that deactivates and re-registers a plugin
  from disk? Or does the existing `restart` console command (which execs
  a fresh interpreter) cover the use case?

- **OQ-7 (two activation paths — which wins?)**: `api_server.run_server`
  and `mcp_server.py` module-level code each independently list the four
  plugins. They can drift. Options: (a) extract a single shared
  `register_first_party_plugins(host)` helper and call it from both; (b)
  leave as-is and rely on code review + tests to keep them in sync; (c)
  have mcp_server import the helper directly from api_server. The spec
  currently mandates they match (R3), but not how that invariant is
  enforced.

---

## Related Artifacts

- Source: `agent/reports/audit-2-architectural-deep-dive.md` §1G, §3
  (leaks #2, #3, #18), §6 (recommendations #1, #2, #4).
- Source code: `src/scenecraft/plugin_host.py`,
  `src/scenecraft/api_server.py:10580–10642`,
  `src/scenecraft/mcp_server.py:60–80`, `src/scenecraft/cli.py:1289`.
- **Divergence from**:
  `/home/prmichaelsen/.acp/projects/scenecraft/agent/specs/local.plugin-host-and-manifest.md`
  — the scenecraft frontend spec assumes **atomic activation** (R31) and
  a **`register_migration`** surface. Neither exists in the engine
  today. This engine spec documents the actual current behavior;
  reconciliation is an open design decision (see OQ-2 and OQ-4).
- Related specs: `local.plugin-api-surface-and-r9a.md` (plugin_api
  surface), `local.engine-server-bootstrap.md` (planned — broader boot
  sequence).
