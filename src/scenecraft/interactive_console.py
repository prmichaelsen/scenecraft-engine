"""Interactive REPL that runs alongside the scenecraft server.

Reads commands from stdin in a daemon thread. Logs from the HTTP /
WebSocket / worker threads keep streaming to stderr independently;
typed input lands on its own lines interleaved with the log stream.

Only starts when stdin is a TTY (skips silently under systemd / pipes).

Available commands:
    h / help / ?          list commands
    r / restart           re-exec the current process with the latest
                          code from disk (kills this PID, spawns a
                          fresh one with identical argv)
    q / quit / exit       graceful shutdown
    s / stats             dump preview cache stats
    w / workers           list active RenderCoordinator workers
    clear                 clear the terminal

Add new commands by registering in the _COMMANDS dict at the bottom.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from typing import Callable


# ── Logging that matches the rest of the server's conventions ────────────


def _log(msg: str) -> None:
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [console] {msg}", file=sys.stderr, flush=True)


# ── Commands ─────────────────────────────────────────────────────────────


def _cmd_help(_: str) -> None:
    _log("commands:")
    for names, (_, doc) in _sorted_commands():
        _log(f"  {', '.join(names):20s}  {doc}")


def _cmd_restart(_: str) -> None:
    """Tear down render workers + ffmpeg subprocesses, then os.execv.

    execv replaces the current process in place — same PID-derived
    resources (ports, stdin/stdout/stderr) become the new interpreter's.
    Everything in memory is dropped. Child subprocesses we opened
    (ffmpeg encoders, proxy-gen transcodes) need to be terminated
    first, otherwise they'd orphan and hold pipe FDs that our new
    process might try to reuse.
    """
    _log("restart: shutting down render workers…")
    try:
        from scenecraft.render.preview_worker import RenderCoordinator
        coord = RenderCoordinator.instance()
        coord.shutdown()  # type: ignore[attr-defined]
    except AttributeError:
        # Older versions don't have a top-level shutdown; iterate
        # workers manually.
        try:
            from scenecraft.render.preview_worker import RenderCoordinator
            coord = RenderCoordinator.instance()
            with coord._lock:  # type: ignore[attr-defined]
                workers = list(coord._workers.values())  # type: ignore[attr-defined]
            for w in workers:
                try:
                    w.stop()
                except Exception:
                    pass
        except Exception as exc:
            _log(f"restart: worker teardown failed: {exc}")
    except Exception as exc:
        _log(f"restart: worker teardown failed: {exc}")

    _log("restart: shutting down proxy coordinator…")
    try:
        from scenecraft.render.proxy_generator import ProxyCoordinator
        ProxyCoordinator.instance().shutdown()
    except Exception as exc:
        _log(f"restart: proxy coordinator shutdown failed: {exc}")

    # Brief pause so subprocesses actually receive their SIGTERM /
    # close-stdin before the new interpreter tries to bind the same
    # port.
    time.sleep(0.2)

    _log(f"restart: execv into fresh {sys.executable} with argv {sys.argv}")
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:
        _log(f"restart: execv FAILED: {exc} — exiting")
        os._exit(1)


def _cmd_quit(_: str) -> None:
    _log("quit: graceful shutdown…")
    # SIGINT lets click/asyncio handlers do their cleanup; if we're
    # hung up on something, SIGTERM a moment later.
    try:
        os.kill(os.getpid(), signal.SIGINT)
    except Exception:
        pass
    # Last resort — give the signal handlers 1s then hard-exit.
    def _force_exit():
        time.sleep(1.0)
        os._exit(0)
    threading.Thread(target=_force_exit, daemon=True).start()


def _cmd_stats(_: str) -> None:
    try:
        from scenecraft.render.frame_cache import global_cache
        from scenecraft.render.fragment_cache import global_fragment_cache
    except Exception as exc:
        _log(f"stats: import failed: {exc}")
        return
    fc = global_cache.stats()
    frag = global_fragment_cache.stats()
    _log(
        f"frame_cache:    {fc['frames']} entries / {fc['bytes']/1e6:.1f} MB "
        f"(hits={fc['hits']}, misses={fc['misses']}, hit_rate={fc['hit_rate']:.1%})"
    )
    _log(
        f"fragment_cache: {frag['fragments']} entries / {frag['bytes']/1e6:.1f} MB "
        f"(hits={frag['hits']}, misses={frag['misses']}, hit_rate={frag['hit_rate']:.1%})"
    )


def _cmd_workers(_: str) -> None:
    try:
        from scenecraft.render.preview_worker import RenderCoordinator
    except Exception as exc:
        _log(f"workers: import failed: {exc}")
        return
    coord = RenderCoordinator.instance()
    with coord._lock:  # type: ignore[attr-defined]
        workers = dict(coord._workers)  # type: ignore[attr-defined]
    if not workers:
        _log("workers: none active")
        return
    _log(f"workers: {len(workers)} active")
    for key, w in workers.items():
        try:
            playing = w._playing.is_set()  # type: ignore[attr-defined]
            playhead = getattr(w, "_playhead_t", 0.0)
            gen = getattr(w, "_encoder_generation", 0)
            bg_q = getattr(getattr(w, "_background_renderer", None), "queue_size", "—")
            _log(
                f"  {key}: playing={playing} playhead={playhead:.2f}s "
                f"enc_gen={gen} bg_queue={bg_q}"
            )
        except Exception as exc:
            _log(f"  {key}: (introspect failed: {exc})")


def _cmd_clear(_: str) -> None:
    # ANSI clear — works on every terminal emulator we care about.
    print("\033[2J\033[H", end="", flush=True)


# ── Dispatcher ───────────────────────────────────────────────────────────


_COMMANDS: dict[str, tuple[Callable[[str], None], str]] = {
    "h": (_cmd_help, "print this list"),
    "help": (_cmd_help, "print this list"),
    "?": (_cmd_help, "print this list"),
    "r": (_cmd_restart, "restart process (execv — picks up latest code on disk)"),
    "restart": (_cmd_restart, "restart process (execv — picks up latest code on disk)"),
    "q": (_cmd_quit, "graceful shutdown"),
    "quit": (_cmd_quit, "graceful shutdown"),
    "exit": (_cmd_quit, "graceful shutdown"),
    "s": (_cmd_stats, "dump preview cache stats (frame + fragment)"),
    "stats": (_cmd_stats, "dump preview cache stats (frame + fragment)"),
    "w": (_cmd_workers, "list active RenderCoordinator workers"),
    "workers": (_cmd_workers, "list active RenderCoordinator workers"),
    "clear": (_cmd_clear, "clear the terminal"),
}


def _sorted_commands() -> list[tuple[tuple[str, ...], tuple[Callable[[str], None], str]]]:
    """Group aliases that dispatch to the same function so `help` prints
    one line per command, not one per alias."""
    groups: dict[int, tuple[list[str], tuple[Callable[[str], None], str]]] = {}
    for name, value in _COMMANDS.items():
        key = id(value[0])
        if key not in groups:
            groups[key] = ([name], value)
        else:
            groups[key][0].append(name)
    out = []
    for names, value in groups.values():
        names.sort(key=lambda s: (len(s), s))
        out.append((tuple(names), value))
    out.sort(key=lambda g: g[0][0])
    return out


# ── REPL thread ──────────────────────────────────────────────────────────


def _repl() -> None:
    _log("interactive console ready — type `help` for commands")
    while True:
        try:
            line = sys.stdin.readline()
        except Exception as exc:
            _log(f"stdin closed: {exc}")
            return
        if not line:
            # EOF — stdin closed (piped input finished). Exit the REPL
            # without killing the server.
            return
        cmd_line = line.strip()
        if not cmd_line:
            continue
        parts = cmd_line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        handler = _COMMANDS.get(cmd)
        if handler is None:
            _log(f"unknown command: {cmd!r} (try `help`)")
            continue
        try:
            handler[0](arg)
        except SystemExit:
            raise
        except Exception as exc:
            import traceback
            _log(f"{cmd}: failed: {exc}\n{traceback.format_exc()}")


def start_if_tty() -> None:
    """Spawn the REPL thread iff stdin is a TTY. No-op otherwise.

    Non-TTY contexts (piped input, systemd service with no stdin)
    skip the console silently — the server runs exactly as before.
    """
    try:
        if not sys.stdin.isatty():
            return
    except Exception:
        return
    t = threading.Thread(target=_repl, name="interactive-console", daemon=True)
    t.start()
