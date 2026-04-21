"""fMP4 fragment encoder for MSE playback streaming.

Encodes a stream of BGR numpy frames into MediaSource-compatible fMP4 fragments.
The first thing a client needs is an init segment (ftyp + moov) that describes
the codec; subsequent media segments (moof + mdat) carry encoded samples.

Implementation strategy:

- PyAV is the primary backend: build a self-contained fMP4 per fragment, then
  split out (ftyp+moov) as init and (moof+mdat) as media. Each fragment is
  encoded in its own container, so DTS starts at 0; we rewrite each media
  segment's tfdt box to advance decode time by the accumulated duration of
  previous fragments.
- If PyAV doesn't validate (detected at __init__ by a smoke encode), we fall
  back to a persistent ffmpeg subprocess that pipes raw BGR frames in and
  writes fMP4 to stdout.

The PyAV path ran in smoke tests with system ffmpeg/libx264 at the time of
writing; the subprocess path is kept for resilience.
"""

from __future__ import annotations

import io
import struct
import subprocess
import threading
from typing import Iterable, Iterator

import numpy as np

# Tune + preset defaults chosen per task spec: ultrafast + zerolatency + crf 23.
DEFAULT_PRESET = "ultrafast"
DEFAULT_TUNE = "zerolatency"
DEFAULT_CRF = 23
DEFAULT_KEYFRAME_INTERVAL = 24  # one GOP per second at 24fps


def _boxes(data: bytes) -> list[tuple[str, int, int]]:
    """Parse top-level ISOBMFF boxes. Returns list of (kind, size, offset)."""
    out: list[tuple[str, int, int]] = []
    offs = 0
    n = len(data)
    while offs + 8 <= n:
        size = int.from_bytes(data[offs : offs + 4], "big")
        kind = data[offs + 4 : offs + 8].decode("ascii", errors="replace")
        out.append((kind, size, offs))
        if size == 0:
            break
        offs += size
    return out


def _split_init_and_media(data: bytes) -> tuple[bytes, bytes]:
    """Split a self-contained fMP4 into (init=ftyp+moov, media=moof+mdat+...).

    Trailing 'mfra' (fragment random-access box) is stripped from media — it is
    only useful for the finalized file and confuses MSE appenders.
    """
    offs = 0
    moov_end: int | None = None
    mfra_off: int | None = None
    for kind, size, off in _boxes(data):
        if kind == "moov":
            moov_end = off + size
        elif kind == "mfra":
            mfra_off = off
            break
    init = data[:moov_end] if moov_end is not None else b""
    end = mfra_off if mfra_off is not None else len(data)
    media = data[moov_end:end] if moov_end is not None else data[:end]
    return init, media


def _find_timescale(init_bytes: bytes) -> int:
    """Extract the video track timescale from an init segment's mdhd box.

    The compositor tracks are encoded at a known fps, but ffmpeg/libx264
    chooses its own timescale (commonly 12288 for 24fps). We parse it from
    init so tfdt patching scales correctly.
    """
    # Find moov > trak > mdia > mdhd
    # Scan for 'mdhd' tag (this is a deep-nested box but the tag is unique enough
    # for a byte-level search in practice).
    i = 0
    while i < len(init_bytes) - 8:
        if init_bytes[i + 4 : i + 8] == b"mdhd":
            version = init_bytes[i + 8]
            # mdhd layout:
            #  - version(1) + flags(3)
            #  - creation_time / mod_time (v0:4/v0:4 or v1:8/v1:8)
            #  - timescale (4)
            if version == 1:
                ts_off = i + 8 + 4 + 8 + 8
            else:
                ts_off = i + 8 + 4 + 4 + 4
            return int.from_bytes(init_bytes[ts_off : ts_off + 4], "big")
        i += 1
    return 12288  # sensible default for 24fps output


def _patch_tfdt(media_bytes: bytes, base_decode_time: int) -> bytes:
    """Rewrite every tfdt box in the media segment to start at base_decode_time.

    tfdt layout:  [size(4)][type='tfdt'][version(1)][flags(3)][decode_time(v0:4 or v1:8)]
    """
    out = bytearray(media_bytes)
    i = 0
    n = len(out)
    while i + 12 <= n:
        if out[i + 4 : i + 8] == b"tfdt":
            version = out[i + 8]
            if version == 1:
                struct.pack_into(">Q", out, i + 12, base_decode_time)
            else:
                struct.pack_into(">I", out, i + 12, base_decode_time & 0xFFFFFFFF)
        i += 1
    return bytes(out)


class FragmentEncoder:
    """Encodes BGR numpy frames into fMP4 init + media segments for MSE.

    Typical usage:

        enc = FragmentEncoder(width=960, height=540, fps=24)
        ws.send(enc.encode_init())
        while playing:
            media = enc.encode_range(next_batch_of_frames)
            ws.send(media)
        enc.close()
    """

    def __init__(
        self,
        width: int,
        height: int,
        fps: float,
        preset: str = DEFAULT_PRESET,
        tune: str = DEFAULT_TUNE,
        crf: int = DEFAULT_CRF,
        keyframe_interval: int = DEFAULT_KEYFRAME_INTERVAL,
        force_backend: str | None = None,
    ) -> None:
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps)
        self.preset = preset
        self.tune = tune
        self.crf = int(crf)
        self.keyframe_interval = int(keyframe_interval)

        # Even dimensions are required by libx264 (yuv420p subsampling).
        if self.width % 2:
            self.width += 1
        if self.height % 2:
            self.height += 1

        self._accumulated_ticks = 0  # tfdt base for next media segment
        self._timescale: int | None = None
        self._init_bytes: bytes | None = None
        self._lock = threading.Lock()

        # Pick backend (PyAV by default; test-pull for fallback).
        if force_backend == "ffmpeg":
            self._use_pyav = False
        elif force_backend == "pyav":
            self._use_pyav = True
        else:
            self._use_pyav = self._pyav_smoke_test()

        # Persistent ffmpeg subprocess state (only used in fallback mode).
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_thread: threading.Thread | None = None
        self._ffmpeg_stdout_chunks: list[bytes] = []
        self._ffmpeg_stdout_cond = threading.Condition()
        self._ffmpeg_closed = False

    # ── PyAV backend ─────────────────────────────────────────────────────

    def _pyav_smoke_test(self) -> bool:
        """Encode 2 frames via PyAV; confirm we get valid ftyp+moov+moof+mdat."""
        try:
            import av  # noqa: F401
        except ImportError:
            return False
        try:
            data = self._pyav_encode_fragment(
                [np.zeros((self.height, self.width, 3), dtype=np.uint8) for _ in range(2)]
            )
        except Exception:
            return False
        kinds = [b[0] for b in _boxes(data)]
        return "ftyp" in kinds and "moov" in kinds and "moof" in kinds and "mdat" in kinds

    def _pyav_encode_fragment(self, frames: list[np.ndarray]) -> bytes:
        """Encode a list of BGR frames into a self-contained fMP4 via PyAV."""
        import av

        buf = io.BytesIO()
        container = av.open(
            buf,
            mode="w",
            format="mp4",
            options={
                "movflags": "+frag_keyframe+empty_moov+default_base_moof",
            },
        )
        # PyAV expects an int (or a Fraction) for rate; cast to avoid numerator attr error.
        stream = container.add_stream("libx264", rate=int(round(self.fps)))
        stream.width = self.width
        stream.height = self.height
        stream.pix_fmt = "yuv420p"
        stream.options = {
            "preset": self.preset,
            "tune": self.tune,
            "crf": str(self.crf),
            # Force a keyframe every `keyframe_interval` frames so fragments
            # start with an IDR and each is independently decodable.
            "g": str(self.keyframe_interval),
            "keyint_min": str(self.keyframe_interval),
            "force_key_frames": "expr:gte(t,n_forced*1)",
        }
        try:
            for arr in frames:
                if arr.shape[0] != self.height or arr.shape[1] != self.width:
                    # Caller is expected to resize; we don't silently resample.
                    # But be lenient: resize via numpy-free path? fall through.
                    pass
                vf = av.VideoFrame.from_ndarray(arr, format="bgr24")
                for pkt in stream.encode(vf):
                    container.mux(pkt)
            for pkt in stream.encode():
                container.mux(pkt)
        finally:
            container.close()
        return buf.getvalue()

    # ── ffmpeg subprocess backend ────────────────────────────────────────

    def _start_ffmpeg(self) -> None:
        """Spawn a persistent ffmpeg process reading raw BGR on stdin, writing fMP4 to stdout."""
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{self.width}x{self.height}",
            "-r",
            str(self.fps),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            self.preset,
            "-tune",
            self.tune,
            "-crf",
            str(self.crf),
            "-g",
            str(self.keyframe_interval),
            "-keyint_min",
            str(self.keyframe_interval),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof+faststart",
            "-f",
            "mp4",
            "-",
        ]
        self._ffmpeg_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._ffmpeg_thread = threading.Thread(
            target=self._ffmpeg_reader, daemon=True
        )
        self._ffmpeg_thread.start()

    def _ffmpeg_reader(self) -> None:
        assert self._ffmpeg_proc is not None
        out = self._ffmpeg_proc.stdout
        assert out is not None
        while True:
            chunk = out.read(65536)
            if not chunk:
                break
            with self._ffmpeg_stdout_cond:
                self._ffmpeg_stdout_chunks.append(chunk)
                self._ffmpeg_stdout_cond.notify_all()

    def _ffmpeg_feed_frames(self, frames: Iterable[np.ndarray]) -> None:
        assert self._ffmpeg_proc is not None and self._ffmpeg_proc.stdin is not None
        for arr in frames:
            if arr.dtype != np.uint8:
                arr = arr.astype(np.uint8)
            if not arr.flags["C_CONTIGUOUS"]:
                arr = np.ascontiguousarray(arr)
            self._ffmpeg_proc.stdin.write(arr.tobytes())

    def _ffmpeg_drain_bytes(self, wait_bytes: int = 0, timeout: float = 5.0) -> bytes:
        """Wait briefly for ffmpeg to emit data, then drain whatever is there."""
        import time
        deadline = time.monotonic() + timeout
        with self._ffmpeg_stdout_cond:
            while sum(len(c) for c in self._ffmpeg_stdout_chunks) < wait_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._ffmpeg_stdout_cond.wait(timeout=min(remaining, 0.1))
            data = b"".join(self._ffmpeg_stdout_chunks)
            self._ffmpeg_stdout_chunks.clear()
        return data

    # ── Public API ───────────────────────────────────────────────────────

    def encode_init(self) -> bytes:
        """Return the fMP4 init segment (ftyp + moov). Idempotent."""
        with self._lock:
            if self._init_bytes is not None:
                return self._init_bytes
            if self._use_pyav:
                # Prime with a single frame of black to force moov emission.
                seed = [np.zeros((self.height, self.width, 3), dtype=np.uint8)]
                data = self._pyav_encode_fragment(seed)
                init, media = _split_init_and_media(data)
                if not init:
                    raise RuntimeError("PyAV produced no init segment")
                self._init_bytes = init
                self._timescale = _find_timescale(init)
                # The media from priming represents 1 frame; we discard it
                # rather than advance the clock, so the first real encode_range
                # starts playback at t=0.
                # Timescale stored for future patches.
                return init
            else:
                # For subprocess backend, we must start ffmpeg first and wait
                # for the ftyp+moov to appear.
                if self._ffmpeg_proc is None:
                    self._start_ffmpeg()
                # Feed a single frame to prime ftyp+moov emission.
                self._ffmpeg_feed_frames([np.zeros((self.height, self.width, 3), dtype=np.uint8)])
                # Give ffmpeg a moment, then drain.
                data = self._ffmpeg_drain_bytes(wait_bytes=32, timeout=3.0)
                boxes = _boxes(data)
                moov_end = None
                for kind, size, off in boxes:
                    if kind == "moov":
                        moov_end = off + size
                        break
                if moov_end is None:
                    # Try harder — encode a couple more frames.
                    self._ffmpeg_feed_frames(
                        [np.zeros((self.height, self.width, 3), dtype=np.uint8) for _ in range(self.keyframe_interval)]
                    )
                    data += self._ffmpeg_drain_bytes(wait_bytes=256, timeout=3.0)
                    boxes = _boxes(data)
                    for kind, size, off in boxes:
                        if kind == "moov":
                            moov_end = off + size
                            break
                if moov_end is None:
                    raise RuntimeError("ffmpeg did not produce moov in time")
                self._init_bytes = data[:moov_end]
                self._timescale = _find_timescale(self._init_bytes)
                # Any media tail from the priming encode is discarded.
                # Note subsequent drains may still include the tail of the priming
                # segment's moof+mdat — we accept and emit it on first encode_range.
                # Save those bytes as "prefix" for encode_range.
                tail = data[moov_end:]
                self._ffmpeg_tail_prefix = tail
                return self._init_bytes

    def encode_range(self, frames: Iterable[np.ndarray]) -> bytes:
        """Encode a sequence of BGR frames into one media segment (moof+mdat).

        Frames must be shape (height, width, 3) uint8 BGR, matching the
        encoder's width/height. Callers are responsible for providing the
        correct count per segment (typically ~1s worth of frames).
        """
        # Materialize — we need len for tfdt advancement.
        frame_list = [f for f in frames]
        if not frame_list:
            return b""
        # Ensure init has been emitted (needed for timescale).
        if self._init_bytes is None:
            self.encode_init()
        assert self._timescale is not None

        with self._lock:
            if self._use_pyav:
                data = self._pyav_encode_fragment(frame_list)
                _, media = _split_init_and_media(data)
                # Advance tfdt to the accumulated decode time.
                patched = _patch_tfdt(media, self._accumulated_ticks)
                duration_ticks = int(round(len(frame_list) * self._timescale / self.fps))
                self._accumulated_ticks += duration_ticks
                return patched
            else:
                # Subprocess mode: feed frames, drain bytes.
                if self._ffmpeg_proc is None:
                    self._start_ffmpeg()
                    # No init was emitted in encode_init (shouldn't reach here).
                self._ffmpeg_feed_frames(frame_list)
                data = self._ffmpeg_drain_bytes(wait_bytes=64, timeout=5.0)
                # Prepend any deferred tail from priming (only on first call).
                tail = getattr(self, "_ffmpeg_tail_prefix", b"")
                if tail:
                    data = tail + data
                    self._ffmpeg_tail_prefix = b""
                # ffmpeg subprocess writes a single fragment per keyframe with
                # correct DTS continuation, so no tfdt patching needed.
                return data

    def fragments(self, frame_iter: Iterable[np.ndarray], frames_per_fragment: int) -> Iterator[bytes]:
        """Convenience iterator: batch frames into fragments of fixed size, yield each."""
        batch: list[np.ndarray] = []
        for arr in frame_iter:
            batch.append(arr)
            if len(batch) >= frames_per_fragment:
                yield self.encode_range(batch)
                batch = []
        if batch:
            yield self.encode_range(batch)

    def close(self) -> None:
        """Release encoder resources."""
        with self._lock:
            if self._ffmpeg_proc is not None:
                try:
                    if self._ffmpeg_proc.stdin:
                        self._ffmpeg_proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._ffmpeg_proc.wait(timeout=2.0)
                except Exception:
                    self._ffmpeg_proc.kill()
                self._ffmpeg_proc = None
            self._ffmpeg_closed = True
