# Project Admin & Diagnostics Console

**Concept**: A scenecraft-specific admin + diagnostics surface — in-process REPL now, optional GUI panel later — exposing ops, inspection, and bulk maintenance via a small grammar of allowlisted commands that speak scenecraft concepts, not Unix concepts.
**Created**: 2026-04-22
**Status**: Proposal

---

## Overview

This document specifies a narrow-purpose command surface for scenecraft: operational controls (restart, cache management), inspection (`inspect tr <id>`, `at <t>`, `why black <t>`), and bulk project maintenance (`regen proxies`, `batch-render <range>`). The surface is delivered in two phases — first as a TTY REPL inside `scenecraft server`, later optionally as a GUI terminal panel in the editor that proxies the same grammar over a REST endpoint.

It is explicitly NOT a general-purpose terminal. No raw shell. No `ls`, no `sql`, no unbounded escape hatches. Commands are named operations with typed arguments, each dispatching to a handler that already has scenecraft domain context. The design goal is "self-serve what you'd otherwise ssh for," scoped to operations the user (whether engineer or — one day — creative end-user) can meaningfully execute and understand.

The REPL portion already ships (commit `1ff70d8` / `3760167` / `601397d` / `4897cd0`) with a small initial command set: `restart`, `quit`, `stats`, `workers`, `help`, `clear`. This design document scopes the next wave of commands and the eventual GUI extension.

---

## Problem Statement

Several pain points have accumulated that an admin console resolves:

1. **Restart cycle friction.** Every time the engine picks up new code or gets stuck, the operator alt-tabs to an ssh session, kills the process, waits for it to release ports + ffmpeg children, and restarts. A `restart` command in the running process eliminates the context switch and reliably tears down subprocesses before execv.

2. **Diagnostic opacity.** When preview playback misbehaves ("why is this region black?", "why is that fragment taking 4 s?"), the path to an answer involves reading stderr, poking at DB state, correlating timestamps, and usually running ad-hoc Python. The person closest to the problem — the person looking at the editor — is the least equipped to diagnose.

3. **Bulk operations are hidden.** Regenerating proxies, clearing caches, forcing a schedule rebuild, pre-rendering a range — these are all achievable today, but only via code paths buried inside API endpoints that aren't wired to a UI. They're effectively unavailable unless the operator writes a Python one-liner.

4. **Future: non-engineer users.** If scenecraft evolves to "customer runs this on a managed instance," those users won't have ssh. A constrained admin surface ("restart", "clear cache", "inspect this clip") serves them too.

**Not a problem this design solves:**
- Agent integration — already handled by MCP
- Event hooks / scripting — already handled by the plugin event system
- Multi-user collaboration — out of scope
- Remote compute / multi-node — speculative, different architecture

---

## Solution

**Design axis 1: named commands, not shell.** Each command is a function with structured args. The dispatcher matches the first whitespace-separated token to a handler name. No interpolation, no composition via pipes. Inspired by the way `pg`, `redis-cli`, and NLE scripting consoles work — a small vocabulary that mirrors domain concepts.

**Design axis 2: two delivery surfaces, one grammar.** The same command table drives:
  - The TTY REPL (daemon thread in `scenecraft server`, already shipped)
  - A future REST endpoint `POST /api/console/exec` that accepts `{cmd, args}` and returns `{stdout, stderr, exit}`
  - A future GUI terminal panel in the editor that's a thin frontend over that endpoint
Adding a command once makes it available everywhere.

**Design axis 3: allowlisted, never escaping.** Commands are registered statically in a `_COMMANDS` dict. No eval, no subprocess invocation with user-supplied argv, no SQL execution. The "escape hatch" is to add a new command to the dict — a code change, reviewable.

**Design axis 4: one-screen output.** Every command fits its primary output on one terminal screen. Long results paginate or tail. `inspect tr <id>` shows one transition's worth of state. `list tr` shows a compact per-row summary, not a dump. This matches how `ps`, `git log`, and friends work — one-screen digestibility is the norm in the tools this feature emulates.

**Alternatives considered and rejected:**
- **Raw shell proxy (`POST /exec cmd=...`)** — RCE by construction. Discarded immediately.
- **`sql>` subcommand** — requires users to know the engine's schema, which nobody outside the maintainer does. The per-entity `inspect` commands cover the motivating use cases without exposing schema.
- **Piping / composition (`list tr | grep trim_in > 0`)** — nice in principle; heavy implementation lift; solves no motivating use case since the commands we have are already entity-scoped.
- **MCP-only** — MCP tools require pre-defining every operation. Terminal-style commands are quicker to add, more ergonomic for the "poke around" workflow. They complement MCP rather than replace it.

---

## Implementation

### Command table

```python
# scenecraft/interactive_console.py (extended)

_COMMANDS: dict[str, tuple[Handler, HelpText]] = {
    # Ops — existing
    "restart": (_cmd_restart, "re-exec process, pick up latest code"),
    "quit":    (_cmd_quit,    "graceful shutdown"),
    "stats":   (_cmd_stats,   "scrub + fragment cache stats"),
    "workers": (_cmd_workers, "active RenderCoordinator workers"),
    "clear":   (_cmd_clear,   "clear terminal"),
    "help":    (_cmd_help,    "print this list"),

    # Inspection (new)
    "inspect": (_cmd_inspect, "inspect <tr|kf|track|pool|project> <id>"),
    "list":    (_cmd_list,    "list <tr|kf|tracks|pool> [--track <id>]"),
    "find":    (_cmd_find,    "find tr --source <path>"),
    "at":      (_cmd_at,      "at <t> — what's visible at time t"),
    "why":     (_cmd_why,     "why black <t> — diagnose why a frame is black"),
    "render-state": (_cmd_render_state, "render-state <project> — text form"),
    "disk":    (_cmd_disk,    "storage breakdown per project"),
    "inspect-media": (_cmd_inspect_media, "inspect-media <source> — ffprobe digest"),

    # Bulk ops (new)
    "regen-proxies":  (_cmd_regen_proxies,  "regen-proxies <project>"),
    "clear-cache":    (_cmd_clear_cache,    "clear-cache <fragment|scrub|both> [<project>]"),
    "invalidate":     (_cmd_invalidate,     "invalidate <project> — wholesale invalidate"),
    "batch-render":   (_cmd_batch_render,   "batch-render <project> [t_start t_end] — bg prerender"),
    "gc-proxies":     (_cmd_gc_proxies,     "gc-proxies [--older-than <days>]"),
    "evict":          (_cmd_evict,          "evict <project> — release worker"),
    "rebuild-schedule": (_cmd_rebuild_schedule, "rebuild-schedule <project>"),
}
```

Each handler takes `(arg: str) -> None`. Output goes to `sys.stderr` via `_log()` for consistency with the surrounding log stream. Commands that produce multi-line output wrap individual lines in `_log()` so the prompt-aware stderr shim handles them correctly.

### Inspection command shapes

```
scenecraft> inspect tr tr_3ff9888b
  id          : tr_3ff9888b
  track       : track_1
  from_kf     : kf_5cfcbeaf  @ 0:00.00  (0.00s)
  to_kf       : kf_0a1481d6  @ 0:05.00  (5.00s)
  duration    : 5.00s
  trim_in     : 0.00
  trim_out    : 0.00
  blend_mode  : normal
  opacity     : 1.0
  effects     : [strobe(freq=8, duty=0.5)]
  selected    : tr_3ff9888b_slot_0.mp4
  candidates  : 4 (0:selected, 1,2,3:alternates)

scenecraft> list tr --track track_1
  id            from     to       dur    candidates  selected
  tr_3ff9...    0:00.00  0:05.00  5.00s   4          slot_0
  tr_7abc...    0:05.00  0:12.30  7.30s   2          slot_1
  tr_f91d...    0:12.30  0:18.00  5.70s   1          slot_0

scenecraft> at 4.2
  base     : tr_3ff9888b (track 1) — proxy active
  overlay  : (none)
  effects  : strobe active (beat t=4.0..4.5)

scenecraft> why black 4.2
  schedule has 1 segment at t=4.2s: tr_3ff9888b (source: /mnt/.../clip.mp4)
  proxy: ✗ missing — generating (queued)
  stream_caps: 0 caps open (worker idle)
  verdict: likely waiting on proxy gen. check `stats` hit rate after 30s.

scenecraft> render-state oktoberfest_show_01
  duration: 8678s   buckets: 4340 @ 2s
  cached:     [██████░░░░░░░░░░░░░░]  42%  (1823 / 4340)
  rendering:  [██░░░░░░░░░░░░░░░░░░]   0.3% (12 / 4340)
  unrendered: [░░░░░░░░░░░░░░░░░░░░]  58%  (2505 / 4340)
  uncached gaps > 30s: 120-214s, 890-1240s, 3200-3800s

scenecraft> disk
  oktoberfest_show_01
    pool         : 2.3 GB
    proxies      : 180 MB
    scrub cache  : 30 MB (in-memory)
    fragment cache: 12 MB (in-memory)
  TOTAL           : 2.5 GB
```

### Bulk-op command shapes

```
scenecraft> regen-proxies oktoberfest_show_01
  enumerated 1 base-track source
  dropping 1 existing proxy... done
  queued proxy gen. monitor via `stats` + `workers`.

scenecraft> clear-cache both oktoberfest_show_01
  fragment cache: dropped 87 entries (312 MB)
  scrub cache:    dropped 412 entries (205 MB)

scenecraft> batch-render oktoberfest_show_01 120 300
  enqueued 90 buckets for background render (t=120..300s)
  priority_bias=-999 (floods ahead of normal playback)

scenecraft> gc-proxies --older-than 30
  found 3 proxies for sources not touched in ≥30 days (420 MB)
  removed 3 proxies (420 MB freed)
```

### Handler implementation pattern

Handlers call existing internals. No new logic beyond argument parsing + pretty-printing. Examples:

- `inspect tr <id>` → `scenecraft.db.get_transition(project_dir, id)` + `get_keyframes` + `get_tr_candidates` → format
- `regen-proxies <project>` → find project_dir, delete proxies directory, call `ProxyCoordinator.instance().ensure_proxy` for each source
- `batch-render <project> [a b]` → resolve worker, call `worker._background_renderer.request_range(a, b, priority_bias=-999)`
- `render-state <project>` → call `snapshot_for_worker(project_dir)` from `render_state.py`, format as text

Most handlers are ~20 lines each. Total code ~400 lines across the extended `interactive_console.py` plus a small formatter module.

### GUI extension (phase 2)

If and when the GUI panel is built:

- New endpoint `POST /api/console/exec` body `{cmd, arg}` → calls the same handler, captures its stdout, returns `{output: str}`
- Auth-gated same as other project endpoints
- Frontend panel: line-edit input + scrolling output pane; shift-enter = multi-line; readline-style history
- Reuses the same `_COMMANDS` table — zero drift between REPL and GUI

### Security constraints

- No command takes arbitrary argv for subprocess invocation. `inspect-media` wraps `ffprobe` with a fixed argv template parameterized only by source path (itself validated against the project's pool).
- Project-scoped commands resolve project_dir via the standard `resolve()` path that already prevents directory traversal.
- No command executes user-supplied code (no eval, no exec, no importlib by path).
- Handlers catch and log their own exceptions — a broken handler can't tombstone the REPL.
- REST endpoint (phase 2) runs handlers synchronously in the HTTP thread; long-running ops should delegate to the existing worker pools rather than block the request.

### Output conventions

- One line per fact. Pretty columnization where natural but no ASCII-art frames.
- `_log()` prefix (`[HH:MM:SS] [console] `) on every output line so output interleaves cleanly with the surrounding log stream and the prompt-aware shim handles redrawing.
- Ranges, durations, IDs truncated sensibly. Full-path values only where necessary.

---

## Key Design Decisions

### Scope

| Decision | Choice | Rationale |
|---|---|---|
| Grammar | Named commands + string arg | Matches user mental model (scenecraft concepts). Cheap to parse, no ambiguity, no RCE. |
| Composition | None (no pipes, no filters) | Motivating use cases are all entity-scoped inspection + bulk ops. Composition is a future escape hatch if we ever need it. |
| Raw shell | Rejected | Per-session trust boundary is the project; shell breaks out of it. |
| SQL subcommand | Rejected | Requires engine-internal schema knowledge. No user meaningfully has it. |

### Audience bifurcation

| Decision | Choice | Rationale |
|---|---|---|
| Primary audience (now) | Engineer operating the server (Patrick, contributors) | Solves documented pain points: restart cycle friction, diagnostic opacity, bulk-op obscurity. |
| Secondary audience (future) | Non-technical end-user on managed instance | A constrained subset (`restart`, `clear cache`, `regen proxies`, `inspect`) remains useful without ssh skill. The full command table stays available for power users. |
| SQL / MCP integration | Explicitly not the audience | MCP tools and plugin events already cover programmatic automation. Console is for humans. |

### Delivery phases

| Decision | Choice | Rationale |
|---|---|---|
| Phase 1 | Extend TTY REPL with new commands | Pure backend work, no frontend build, immediate value. Ships one PR at a time (one command family per PR is reasonable). |
| Phase 2 | REST endpoint + GUI terminal panel | Deferred until the REPL grammar is settled and actual demand materializes. Frontend cost is real; don't front-load. |

### Command grammar notes

| Decision | Choice | Rationale |
|---|---|---|
| Arg passing | Single string arg, whitespace-split by handler | Simplest dispatch. Handlers that need multi-arg do their own split. |
| Flags | Ad-hoc per-handler (e.g., `list tr --track X`) | Not worth a full argparse layer; `--k v` is pattern-matched where needed. |
| Output destination | stderr via `_log()` | Consistency with surrounding log stream; prompt-aware shim handles redraw. |
| Error handling | Try/except in dispatcher; REPL never dies from a command | Crashes mid-session would be awful UX. |

### Out of scope (deliberately)

- Event subscription / hooks (→ plugin system)
- Agent-driven automation (→ MCP tools)
- Multi-user collaboration (→ not a product priority)
- Remote/multi-node commanding (→ speculative architecture)
- Piping, filters, composition (→ later if demand materializes)

---

## Open Questions

- **Long-running ops feedback.** `regen-proxies` kicks off a 10-minute transcode. How does the REPL indicate progress? Options: (a) fire-and-forget + user checks `stats`, (b) block the REPL with progress updates, (c) launch in background and emit log lines as proxies land. Leaning (c) — the prompt-aware shim makes interleaved log lines natural.

- **Multi-line output pagination.** `list tr` on a huge project may be hundreds of rows. Cap at ~50 rows with a "(N more — use `--all` to show all)" footer? Or leave it to the terminal to scroll? Leaning cap + `--all`.

- **Project scoping default.** Most project-scoped commands need a project argument. If there's only one active worker, should the command default to that project? Leaning yes, with an "(assumed project=X)" note.

- **History / readline persistence.** The existing REPL has in-memory readline history (via `input()`). Persist across restarts to `~/.scenecraft/console_history`? Nice-to-have, defer.

- **Completion.** Tab-completion of command names + project names would be slick. `readline.set_completer`. Nice-to-have, defer.

---

## Related

- **Design:** `local.backend-rendered-preview-streaming.md` — the preview pipeline the console primarily observes
- **Implementation:** `src/scenecraft/interactive_console.py` — current REPL
- **Tasks:** M12 preview work; console commands piggyback on primitives from M12
