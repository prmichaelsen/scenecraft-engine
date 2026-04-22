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

# readline gives us access to the in-flight input buffer — needed to
# redraw "scenecraft> <partial input>" after a log line clears it.
try:
    import readline as _readline
except ImportError:
    _readline = None  # type: ignore[assignment]


_PROMPT_TEXT = "scenecraft> "
_io_lock = threading.Lock()
_repl_active = False  # True while the REPL is running and owns the prompt line


# ── Terminal mode management ─────────────────────────────────────────────


def _ensure_cooked_mode() -> tuple[Callable[[], None], bool]:
    """Force the TTY into canonical/cooked mode with echo + CR→LF.

    Some environments leave the terminal in raw / non-canonical mode
    (a prior program crashed without restoring termios, or the shell
    set it up oddly). In that mode Enter sends `\\r` and the terminal
    echoes it as `^M` instead of completing the input line.

    Returns (restore_fn, changed) — call restore_fn when the REPL
    exits to put the terminal back the way we found it.
    """
    noop = (lambda: None)
    try:
        import termios
        import tty
    except Exception:
        return noop, False
    try:
        fd = sys.stdin.fileno()
    except Exception:
        return noop, False
    try:
        saved = termios.tcgetattr(fd)
    except Exception:
        return noop, False

    # Set the input flags we care about without stomping on unrelated
    # ones (e.g. baud, character size).
    new = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = new
    iflag |= termios.ICRNL      # translate CR (Enter) to LF on input
    lflag |= termios.ICANON     # line-at-a-time input
    lflag |= termios.ECHO       # echo typed characters
    lflag |= termios.ECHOE      # echo erase as backspace-space-backspace
    lflag |= termios.ECHOK      # echo kill char
    new = [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]

    try:
        termios.tcsetattr(fd, termios.TCSANOW, new)
    except Exception:
        return noop, False

    def _restore() -> None:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
        except Exception:
            pass

    return _restore, True


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
    global _repl_active
    restore_tty, tty_changed = _ensure_cooked_mode()
    if tty_changed:
        _log("terminal: forced canonical mode (ICANON | ICRNL | ECHO)")
    restore_stderr = _install_prompt_aware_stderr()
    _repl_active = True
    _log("interactive console ready — type `help` for commands (press Enter to submit)")
    # input() is more reliable than sys.stdin.readline() across terminals —
    # handles cooked-mode line buffering the way the user expects, echoes
    # typed characters, and handles Ctrl-D / Ctrl-C more predictably.
    try:
        while True:
            try:
                line = input("scenecraft> ")
            except EOFError:
                _log("stdin EOF — console exiting (server still running)")
                return
            except KeyboardInterrupt:
                print()
                continue
            except Exception as exc:
                _log(f"readline error: {exc}")
                time.sleep(0.5)
                continue
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
    finally:
        _repl_active = False
        restore_stderr()
        restore_tty()


# ── Prompt-aware log interleaving ─────────────────────────────────────────


class _PromptAwareStream:
    """Wraps stderr so log writes don't trample an in-flight typed prompt.

    When a write arrives:
      1. ANSI carriage-return + clear-to-end-of-line wipes the current
         line (which is either blank or the live prompt + partial input).
      2. The log bytes go out, ending in newline so the terminal advances.
      3. The prompt is re-emitted with the in-flight input buffer pulled
         from readline. Cursor lands after that buffer — readline will
         reconcile cursor state on the next keystroke via its redisplay.

    Only active while the REPL is running (``_repl_active``). Before then
    (startup) and after (REPL exited), writes pass through untouched.
    """

    def __init__(self, underlying) -> None:
        self._u = underlying

    def write(self, data: str) -> int:
        if not data:
            return 0
        if not _repl_active:
            return self._u.write(data)
        try:
            if not self._u.isatty():
                return self._u.write(data)
        except Exception:
            return self._u.write(data)

        with _io_lock:
            buffer = ""
            if _readline is not None:
                try:
                    buffer = _readline.get_line_buffer()
                except Exception:
                    pass
            # \r  — move cursor to start of line
            # \x1b[2K — ANSI "erase entire line"
            self._u.write("\r\x1b[2K")
            self._u.write(data)
            if not data.endswith("\n"):
                self._u.write("\n")
            self._u.write(_PROMPT_TEXT + buffer)
            self._u.flush()
        return len(data)

    def flush(self) -> None:
        try:
            self._u.flush()
        except Exception:
            pass

    def __getattr__(self, name):  # fall through for isatty, fileno, etc.
        return getattr(self._u, name)


def _install_prompt_aware_stderr() -> Callable[[], None]:
    """Replace sys.stderr with the prompt-aware wrapper.

    Returns a restore function. No-op if stderr isn't a TTY.
    """
    noop = (lambda: None)
    try:
        if not sys.stderr.isatty():
            return noop
    except Exception:
        return noop
    original = sys.stderr
    sys.stderr = _PromptAwareStream(original)  # type: ignore[assignment]

    def _restore() -> None:
        sys.stderr = original  # type: ignore[assignment]

    return _restore


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
