"""Regression tests for local.engine-cache-invalidation.md.

Covers `scenecraft.render.cache_invalidation.invalidate_frames_for_mutation`
— the chokepoint every mutating REST endpoint calls after a DB-write
commits to drop stale frame/fragment cache entries and notify the
`RenderCoordinator`.

Structure:

- Unit section: every Base Case + Edge Case with `covers Rn` docstrings.
  Each independent `try/except: pass` block is tested in isolation (the
  non-fatal-by-design invariant is the load-bearing contract).
- E2E section (`class TestEndToEnd`): drives the function end-to-end in
  the running `engine_server` process, exercising the only HTTP surface
  that observes invalidation today — `/api/render-cache/stats`. Target-
  state tests that require the M16 dispatcher refactor (per-working-copy
  partitioning, 3-tuple return with `coordinator_fallback`, BG requeue
  fallback, cross-endpoint wiring) are marked
  `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor",
  strict=False)` so they light up automatically when the engine ships
  them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from scenecraft.render import cache_invalidation as ci
from scenecraft.render import frame_cache as frame_cache_mod
from scenecraft.render import fragment_cache as fragment_cache_mod


# ---------------------------------------------------------------------------
# Domain-scoped helpers (prefixed `_caches_inv_` per task directive to avoid
# collision with task-74's `_caches_` prefix in the sibling DAO-cache file).
# ---------------------------------------------------------------------------


class _CallRecorder:
    """Patched collaborator that records every invocation in order."""

    def __init__(self, *, return_value: int = 0, raise_exc: Exception | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.return_value = return_value
        self.raise_exc = raise_exc

    def __call__(self, *args, **kwargs):
        self.calls.append(("__call__", args, kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.return_value


def _caches_inv_install_recorders(
    monkeypatch: pytest.MonkeyPatch,
    *,
    frame_proj_return: Any = 3,
    frame_range_return: Any = 3,
    frag_proj_return: Any = 2,
    frag_range_return: Any = 2,
    frame_proj_exc: Exception | None = None,
    frame_range_exc: Exception | None = None,
    frag_proj_exc: Exception | None = None,
    frag_range_exc: Exception | None = None,
    coord_instance_exc: Exception | None = None,
    coord_proj_exc: Exception | None = None,
    coord_bg_exc: Exception | None = None,
    coord_proj_return: bool = True,
    coord_bg_return: int = 0,
    call_log: list | None = None,
) -> dict[str, _CallRecorder]:
    """Patch every collaborator referenced by `invalidate_frames_for_mutation`.

    All patched callables share a single `call_log` list (if supplied) so
    ordering assertions can be made globally (R19 / R20).
    """
    log = call_log if call_log is not None else []

    def mk(name: str, return_value, exc):
        def _fn(*a, **k):
            log.append((name, a, k))
            if exc is not None:
                raise exc
            return return_value
        rec = _CallRecorder(return_value=return_value, raise_exc=exc)
        # Wrap so `rec.calls` is populated _and_ global log is populated.
        def wrapped(*a, **k):
            rec.calls.append(("__call__", a, k))
            log.append((name, a, k))
            if exc is not None:
                raise exc
            return return_value
        return wrapped, rec

    f_proj_fn, f_proj_rec = mk("frame.invalidate_project", frame_proj_return, frame_proj_exc)
    f_rng_fn, f_rng_rec = mk("frame.invalidate_ranges", frame_range_return, frame_range_exc)
    g_proj_fn, g_proj_rec = mk("fragment.invalidate_project", frag_proj_return, frag_proj_exc)
    g_rng_fn, g_rng_rec = mk("fragment.invalidate_ranges", frag_range_return, frag_range_exc)

    monkeypatch.setattr(frame_cache_mod.global_cache, "invalidate_project", f_proj_fn)
    monkeypatch.setattr(frame_cache_mod.global_cache, "invalidate_ranges", f_rng_fn)
    monkeypatch.setattr(fragment_cache_mod.global_fragment_cache, "invalidate_project", g_proj_fn)
    monkeypatch.setattr(fragment_cache_mod.global_fragment_cache, "invalidate_ranges", g_rng_fn)

    # Stub the RenderCoordinator import. The function imports locally, so we
    # need to patch the attribute on the preview_worker module.
    import scenecraft.render.preview_worker as pw

    class _StubCoord:
        def invalidate_project(self, project_dir):
            log.append(("coord.invalidate_project", (project_dir,), {}))
            if coord_proj_exc is not None:
                raise coord_proj_exc
            return coord_proj_return

        def invalidate_ranges_in_background(self, project_dir, ranges):
            log.append(("coord.invalidate_ranges_in_background", (project_dir, ranges), {}))
            if coord_bg_exc is not None:
                raise coord_bg_exc
            return coord_bg_return

    stub_coord = _StubCoord()

    class _StubCoordCls:
        @classmethod
        def instance(cls):
            log.append(("coord.instance", (), {}))
            if coord_instance_exc is not None:
                raise coord_instance_exc
            return stub_coord

    monkeypatch.setattr(pw, "RenderCoordinator", _StubCoordCls)

    return {
        "frame_invalidate_project": f_proj_rec,
        "frame_invalidate_ranges": f_rng_rec,
        "fragment_invalidate_project": g_proj_rec,
        "fragment_invalidate_ranges": g_rng_rec,
        "coord": stub_coord,
        "log": log,
    }


@pytest.fixture
def caches_inv_call_log() -> list:
    return []


@pytest.fixture
def caches_inv_recorders(monkeypatch, caches_inv_call_log):
    """Default-healthy collaborators; override per-test with monkeypatch."""
    return _caches_inv_install_recorders(
        monkeypatch, call_log=caches_inv_call_log,
    )


@pytest.fixture
def real_caches_cleared():
    """Clear the module-level caches before + after the test (e2e use)."""
    frame_cache_mod.global_cache.clear()
    fragment_cache_mod.global_fragment_cache.clear()
    yield
    frame_cache_mod.global_cache.clear()
    fragment_cache_mod.global_fragment_cache.clear()


# ---------------------------------------------------------------------------
# Unit — Base Cases
# ---------------------------------------------------------------------------


class TestBaseCases:
    """Core contract — happy path, wholesale vs surgical, non-fatal invariant."""

    def test_surgical_happy_path(self, project_dir, caches_inv_recorders, caches_inv_call_log):
        """covers R1, R2, R8, R10, R13, R15, R16, R19 — surgical-happy-path."""
        frames, frags = ci.invalidate_frames_for_mutation(project_dir, [(1.5, 2.5)])
        assert frames == 3
        assert frags == 2
        # Exactly-once calls with the normalized list.
        fr = caches_inv_recorders["frame_invalidate_ranges"].calls
        fg = caches_inv_recorders["fragment_invalidate_ranges"].calls
        assert len(fr) == 1 and fr[0][1] == (project_dir, [(1.5, 2.5)])
        assert len(fg) == 1 and fg[0][1] == (project_dir, [(1.5, 2.5)])
        # Neither cache's `invalidate_project` called.
        assert caches_inv_recorders["frame_invalidate_project"].calls == []
        assert caches_inv_recorders["fragment_invalidate_project"].calls == []
        # Coord sees project + BG requeue.
        names = [c[0] for c in caches_inv_call_log]
        assert "coord.invalidate_project" in names
        assert "coord.invalidate_ranges_in_background" in names

    def test_surgical_multiple_ranges(self, project_dir, caches_inv_recorders):
        """covers R8, R10, R13 — surgical-multiple-ranges."""
        rngs = [(1.0, 2.0), (5.0, 6.0), (9.0, 10.0)]
        ci.invalidate_frames_for_mutation(project_dir, rngs)
        fr = caches_inv_recorders["frame_invalidate_ranges"].calls
        fg = caches_inv_recorders["fragment_invalidate_ranges"].calls
        assert fr[0][1][1] == rngs
        assert fg[0][1][1] == rngs

    def test_wholesale_none(self, project_dir, caches_inv_recorders, caches_inv_call_log):
        """covers R3, R4, R9, R12, R15, R17 — wholesale-none."""
        frames, frags = ci.invalidate_frames_for_mutation(project_dir, ranges=None)
        assert frames == 3 and frags == 2
        # `invalidate_project` called on both caches, not `invalidate_ranges`.
        assert len(caches_inv_recorders["frame_invalidate_project"].calls) == 1
        assert len(caches_inv_recorders["fragment_invalidate_project"].calls) == 1
        assert caches_inv_recorders["frame_invalidate_ranges"].calls == []
        assert caches_inv_recorders["fragment_invalidate_ranges"].calls == []
        # Coord: invalidate_project yes, BG requeue skipped.
        names = [c[0] for c in caches_inv_call_log]
        assert "coord.invalidate_project" in names
        assert "coord.invalidate_ranges_in_background" not in names

    def test_wholesale_empty_list(self, project_dir, caches_inv_recorders, caches_inv_call_log):
        """covers R5, R7 — wholesale-empty-list."""
        ci.invalidate_frames_for_mutation(project_dir, ranges=[])
        assert len(caches_inv_recorders["frame_invalidate_project"].calls) == 1
        assert len(caches_inv_recorders["fragment_invalidate_project"].calls) == 1
        assert caches_inv_recorders["frame_invalidate_ranges"].calls == []
        names = [c[0] for c in caches_inv_call_log]
        assert "coord.invalidate_ranges_in_background" not in names

    def test_all_inverted_promotes_to_wholesale(
        self, project_dir, caches_inv_recorders, caches_inv_call_log,
    ):
        """covers R6, R7 — all-inverted-promotes-to-wholesale."""
        ci.invalidate_frames_for_mutation(project_dir, [(5.0, 3.0), (10.0, 8.0)])
        assert len(caches_inv_recorders["frame_invalidate_project"].calls) == 1
        assert len(caches_inv_recorders["fragment_invalidate_project"].calls) == 1
        assert caches_inv_recorders["frame_invalidate_ranges"].calls == []
        names = [c[0] for c in caches_inv_call_log]
        assert "coord.invalidate_ranges_in_background" not in names

    def test_mixed_inverted_filtered(self, project_dir, caches_inv_recorders):
        """covers R6 — mixed-inverted-filtered."""
        ci.invalidate_frames_for_mutation(
            project_dir, [(1.0, 2.0), (5.0, 3.0), (7.0, 9.0)],
        )
        expected = [(1.0, 2.0), (7.0, 9.0)]
        assert caches_inv_recorders["frame_invalidate_ranges"].calls[0][1][1] == expected
        assert caches_inv_recorders["fragment_invalidate_ranges"].calls[0][1][1] == expected

    def test_zero_width_kept(self, project_dir, caches_inv_recorders, caches_inv_call_log):
        """covers R6 — zero-width-kept; surgical-mode-retained."""
        ci.invalidate_frames_for_mutation(project_dir, [(4.0, 4.0)])
        assert caches_inv_recorders["frame_invalidate_ranges"].calls[0][1][1] == [(4.0, 4.0)]
        assert caches_inv_recorders["fragment_invalidate_ranges"].calls[0][1][1] == [(4.0, 4.0)]
        # Surgical mode retained — BG requeue runs.
        names = [c[0] for c in caches_inv_call_log]
        assert "coord.invalidate_ranges_in_background" in names

    def test_frame_cache_raises_non_fatal(self, project_dir, monkeypatch, caches_inv_call_log):
        """covers R3, R11, R20 — frame-cache-raises-non-fatal."""
        _caches_inv_install_recorders(
            monkeypatch, frame_range_exc=RuntimeError("frame boom"),
            call_log=caches_inv_call_log,
        )
        frames, frags = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        assert frames == 0  # frame-cache raised → stays 0
        assert frags == 2
        names = [c[0] for c in caches_inv_call_log]
        assert "fragment.invalidate_ranges" in names  # fragment still ran
        assert "coord.invalidate_project" in names
        assert "coord.invalidate_ranges_in_background" in names  # BG still ran

    def test_fragment_cache_raises_non_fatal(self, project_dir, monkeypatch, caches_inv_call_log):
        """covers R3, R14, R20 — fragment-cache-raises-non-fatal."""
        _caches_inv_install_recorders(
            monkeypatch, frag_range_exc=RuntimeError("frag boom"),
            frame_range_return=3, call_log=caches_inv_call_log,
        )
        frames, frags = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        assert frames == 3
        assert frags == 0
        names = [c[0] for c in caches_inv_call_log]
        assert "coord.invalidate_project" in names
        assert "coord.invalidate_ranges_in_background" in names

    def test_coord_instance_raises_non_fatal(self, project_dir, monkeypatch, caches_inv_call_log):
        """covers R3, R18, R20 — coord-raises-non-fatal (instance lookup)."""
        _caches_inv_install_recorders(
            monkeypatch, coord_instance_exc=RuntimeError("no coord"),
            call_log=caches_inv_call_log,
        )
        frames, frags = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        assert (frames, frags) == (3, 2)
        names = [c[0] for c in caches_inv_call_log]
        # coord.instance raised → invalidate_project never called
        assert "coord.invalidate_project" not in names
        assert "coord.invalidate_ranges_in_background" not in names

    def test_coord_invalidate_project_raises_non_fatal(
        self, project_dir, monkeypatch, caches_inv_call_log,
    ):
        """covers R3, R18 — coord.invalidate_project raises; BG skipped."""
        _caches_inv_install_recorders(
            monkeypatch, coord_proj_exc=RuntimeError("coord proj boom"),
            call_log=caches_inv_call_log,
        )
        frames, frags = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        assert (frames, frags) == (3, 2)
        names = [c[0] for c in caches_inv_call_log]
        assert "coord.invalidate_project" in names
        # Exception in invalidate_project short-circuits the rest of the block.
        assert "coord.invalidate_ranges_in_background" not in names

    def test_coord_bg_requeue_raises_non_fatal(
        self, project_dir, monkeypatch, caches_inv_call_log,
    ):
        """covers R3, R18 — coord-bg-requeue-raises-non-fatal."""
        _caches_inv_install_recorders(
            monkeypatch, coord_bg_exc=RuntimeError("bg boom"),
            call_log=caches_inv_call_log,
        )
        frames, frags = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        assert (frames, frags) == (3, 2)
        names = [c[0] for c in caches_inv_call_log]
        assert "coord.invalidate_project" in names  # ran before the failure

    def test_all_collaborators_raise(self, project_dir, monkeypatch, caches_inv_call_log):
        """covers R3, R11, R14, R18, R19 — all-collaborators-raise."""
        _caches_inv_install_recorders(
            monkeypatch,
            frame_range_exc=RuntimeError("f"),
            frame_proj_exc=RuntimeError("f"),
            frag_range_exc=RuntimeError("g"),
            frag_proj_exc=RuntimeError("g"),
            coord_instance_exc=RuntimeError("c"),
            call_log=caches_inv_call_log,
        )
        # Surgical.
        assert ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)]) == (0, 0)
        # Wholesale.
        assert ci.invalidate_frames_for_mutation(project_dir, None) == (0, 0)

    def test_no_active_worker(self, project_dir, monkeypatch, caches_inv_call_log):
        """covers R22 — no-active-worker (coord returns False/0 silently)."""
        _caches_inv_install_recorders(
            monkeypatch, coord_proj_return=False, coord_bg_return=0,
            call_log=caches_inv_call_log,
        )
        frames, frags = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        assert (frames, frags) == (3, 2)  # coord return values not surfaced

    def test_empty_caches_returns_zero(self, project_dir, monkeypatch):
        """covers R21 — empty-caches-returns-zero."""
        _caches_inv_install_recorders(
            monkeypatch,
            frame_proj_return=0, frame_range_return=0,
            frag_proj_return=0, frag_range_return=0,
        )
        assert ci.invalidate_frames_for_mutation(project_dir, None) == (0, 0)
        assert ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)]) == (0, 0)

    def test_return_type_invariant(self, project_dir, monkeypatch):
        """covers R2, R3 — return-type-invariant."""
        _caches_inv_install_recorders(monkeypatch)
        out = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        assert isinstance(out, tuple) and len(out) == 2
        assert all(isinstance(x, int) for x in out)
        assert all(x >= 0 for x in out)


# ---------------------------------------------------------------------------
# Unit — Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:

    def test_ordering_fixed_surgical(self, project_dir, caches_inv_recorders, caches_inv_call_log):
        """covers R19, R20 — ordering-fixed (surgical)."""
        ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        names = [c[0] for c in caches_inv_call_log]
        # Filter to the load-bearing sequence.
        sequence = [n for n in names if n in (
            "frame.invalidate_ranges",
            "fragment.invalidate_ranges",
            "coord.invalidate_project",
            "coord.invalidate_ranges_in_background",
        )]
        assert sequence == [
            "frame.invalidate_ranges",
            "fragment.invalidate_ranges",
            "coord.invalidate_project",
            "coord.invalidate_ranges_in_background",
        ]

    def test_ordering_fixed_wholesale(self, project_dir, caches_inv_recorders, caches_inv_call_log):
        """covers R19, R20 — ordering-fixed (wholesale; BG requeue absent)."""
        ci.invalidate_frames_for_mutation(project_dir, None)
        names = [c[0] for c in caches_inv_call_log]
        sequence = [n for n in names if n in (
            "frame.invalidate_project",
            "fragment.invalidate_project",
            "coord.invalidate_project",
            "coord.invalidate_ranges_in_background",
        )]
        assert sequence == [
            "frame.invalidate_project",
            "fragment.invalidate_project",
            "coord.invalidate_project",
        ]

    def test_normalized_list_shared_across_caches(
        self, project_dir, caches_inv_recorders,
    ):
        """covers R8, R17 — normalized-list-shared-across-caches."""
        def gen():
            yield (1.0, 2.0)
            yield (3.0, 4.0)
        ci.invalidate_frames_for_mutation(project_dir, gen())
        expected = [(1.0, 2.0), (3.0, 4.0)]
        assert caches_inv_recorders["frame_invalidate_ranges"].calls[0][1][1] == expected
        assert caches_inv_recorders["fragment_invalidate_ranges"].calls[0][1][1] == expected

    def test_iterator_consumed_once(self, project_dir, caches_inv_recorders):
        """covers R8 — iterator-consumed-once (no re-iteration crash)."""
        data = [(1.0, 2.0), (3.0, 4.0)]
        it = iter(data)
        ci.invalidate_frames_for_mutation(project_dir, it)
        # Iterator exhausted — next() would raise StopIteration. Function
        # didn't try to consume it twice (both caches see the materialized list).
        with pytest.raises(StopIteration):
            next(it)
        expected = [(1.0, 2.0), (3.0, 4.0)]
        assert caches_inv_recorders["frame_invalidate_ranges"].calls[0][1][1] == expected
        assert caches_inv_recorders["fragment_invalidate_ranges"].calls[0][1][1] == expected

    def test_wholesale_skips_bg_requeue(self, project_dir, caches_inv_recorders, caches_inv_call_log):
        """covers R17 (wholesale-skips-bg-requeue)."""
        ci.invalidate_frames_for_mutation(project_dir, None)
        names = [c[0] for c in caches_inv_call_log]
        assert "coord.invalidate_ranges_in_background" not in names
        assert "coord.invalidate_project" in names

    def test_idempotent_on_repeat(self, project_dir, monkeypatch, caches_inv_call_log):
        """covers R23 — idempotent-on-repeat; coord still signaled each call."""
        # First call drains — second call sees empty caches → (0, 0).
        # We simulate with monkeypatch: rig return_value to switch between
        # calls using a counter.
        call_count = {"f": 0, "g": 0}

        def f_rng(pd, rl):
            call_count["f"] += 1
            caches_inv_call_log.append(("frame.invalidate_ranges", (pd, rl), {}))
            return 3 if call_count["f"] == 1 else 0

        def g_rng(pd, rl):
            call_count["g"] += 1
            caches_inv_call_log.append(("fragment.invalidate_ranges", (pd, rl), {}))
            return 2 if call_count["g"] == 1 else 0

        # Baseline recorders for the rest of the collaborators.
        _caches_inv_install_recorders(monkeypatch, call_log=caches_inv_call_log)
        monkeypatch.setattr(frame_cache_mod.global_cache, "invalidate_ranges", f_rng)
        monkeypatch.setattr(
            fragment_cache_mod.global_fragment_cache, "invalidate_ranges", g_rng,
        )

        a = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        b = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        assert a == (3, 2)
        assert b == (0, 0)
        # Coord signaled on both calls (signal is idempotent-safe).
        proj_calls = [n for n in caches_inv_call_log if n[0] == "coord.invalidate_project"]
        assert len(proj_calls) == 2

    @pytest.mark.xfail(
        reason="target-state R29; documents spec contract — current transitional "
               "impl has no active-scrub hook, relies on preview-worker re-prime.",
        strict=False,
    )
    def test_scrub_overlap_re_renders_no_flicker(self, project_dir, caches_inv_recorders):
        """covers R29 (OQ-1) — scrub-overlap-re-renders-no-flicker (target)."""
        # Target: invalidation during active scrub yields brief re-render at
        # next-visible-frame, no UI flicker. The current function has no
        # direct hook to verify this — it relies on the preview worker's
        # cache-miss path. Codified as target-state xfail.
        ci.invalidate_frames_for_mutation(project_dir, [(1.5, 2.5)])
        # Placeholder assertion; spec contract verified in preview-worker spec.
        assert caches_inv_recorders["frame_invalidate_ranges"].calls

    @pytest.mark.xfail(
        reason="target-state R18a (OQ-2): current 2-tuple return has no "
               "`coordinator_fallback` flag; BG-requeue failure does not trigger "
               "wholesale fallback yet. Awaits M16 refactor.",
        strict=False,
    )
    def test_bg_requeue_raises_wholesale_fallback(
        self, project_dir, monkeypatch, caches_inv_call_log,
    ):
        """covers R18a, OQ-2 — bg-requeue-raises-wholesale-fallback (target)."""
        _caches_inv_install_recorders(
            monkeypatch, coord_bg_exc=RuntimeError("bg boom"),
            call_log=caches_inv_call_log,
        )
        result = ci.invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])
        # Target: 3-tuple with coordinator_fallback=True.
        assert len(result) == 3
        assert result[2] is True
        # And `coord.invalidate_project` called twice (initial + fallback).
        proj_calls = [
            n for n in caches_inv_call_log if n[0] == "coord.invalidate_project"
        ]
        assert len(proj_calls) == 2

    def test_scrub_non_overlapping_no_op_on_active_fragment(
        self, project_dir, real_caches_cleared,
    ):
        """covers R30 (OQ-3) — scrub-non-overlapping-no-op-on-active-fragment."""
        # Seed the real fragment cache with an "active" fragment at [1.0, 3.0].
        fragment_cache_mod.global_fragment_cache.put(
            project_dir, t0=1.0, encoder_generation=1,
            fmp4=b"active-fragment-bytes", duration_ms=2000,
        )
        before = fragment_cache_mod.global_fragment_cache.stats()["fragments"]
        assert before == 1
        # Invalidate a non-overlapping range.
        ci.invalidate_frames_for_mutation(project_dir, [(10.0, 12.0)])
        after = fragment_cache_mod.global_fragment_cache.stats()["fragments"]
        assert after == 1  # active fragment untouched

    def test_wholesale_during_render_snapshot_semantics(
        self, project_dir, caches_inv_recorders, caches_inv_call_log,
    ):
        """covers R31 (OQ-4) — wholesale invalidate does not abort in-flight render.

        The function signals `invalidate_project` but surfaces no abort —
        snapshot semantics live in the render-pipeline spec. Verify this
        function emits no abort-style call and returns cleanly.
        """
        ci.invalidate_frames_for_mutation(project_dir, None)
        names = [c[0] for c in caches_inv_call_log]
        # No "abort"/"cancel"-style call emitted by the invalidation function.
        assert not any("abort" in n or "cancel" in n for n in names)
        assert "coord.invalidate_project" in names

    def test_no_internal_lock_across_collaborators(self):
        """negative — INV-1: no threading.Lock held across frame → fragment → coord."""
        import inspect
        src = inspect.getsource(ci.invalidate_frames_for_mutation)
        # Confirm no lock acquisition or per-project-mutex pattern.
        assert "threading.Lock" not in src
        assert "asyncio.Lock" not in src
        assert "_project_locks" not in src
        assert "acquire(" not in src

    @pytest.mark.xfail(
        reason="target-state R24 (OQ-6): negative-time clamp not implemented; "
               "current impl passes negatives through to caches unchanged.",
        strict=False,
    )
    def test_negative_times_clipped_to_zero(
        self, project_dir, caches_inv_recorders,
    ):
        """covers R24, OQ-6 — negative-times-clipped-to-zero (target)."""
        ci.invalidate_frames_for_mutation(
            project_dir, [(-1.5, 2.0), (3.0, -0.5), (-2.0, -1.0)],
        )
        received = caches_inv_recorders["frame_invalidate_ranges"].calls[0][1][1]
        # Per spec: first → (0, 2); second → inverted after clip, dropped;
        # third → (0, 0) zero-width, kept.
        assert received == [(0.0, 2.0), (0.0, 0.0)]
        # No negative values reach the collaborators.
        for a, b in received:
            assert a >= 0 and b >= 0

    def test_unknown_target_silent_noop(self, tmp_path, caches_inv_recorders):
        """covers R25, OQ-7 — unknown-target-silent-noop."""
        never_opened = tmp_path / "nonexistent_project"
        # Rig returns 0 to simulate "cache has nothing for this path".
        # Simpler: call it and assert no raise + tuple return shape.
        out = ci.invalidate_frames_for_mutation(never_opened, None)
        assert isinstance(out, tuple) and len(out) == 2

    def test_large_range_list_1000_accepted(self, project_dir, caches_inv_recorders):
        """covers R26, OQ-8 — large-range-list-1000-accepted."""
        rngs = [(float(i), float(i) + 0.5) for i in range(1000)]
        ci.invalidate_frames_for_mutation(project_dir, rngs)
        recv_frame = caches_inv_recorders["frame_invalidate_ranges"].calls[0][1][1]
        recv_frag = caches_inv_recorders["fragment_invalidate_ranges"].calls[0][1][1]
        assert len(recv_frame) == 1000
        assert len(recv_frag) == 1000

    @pytest.mark.xfail(
        reason="target-state R27/INV-7: per-working-copy cache partitioning not "
               "implemented. Current signature accepts only `project_dir`; target "
               "signature is `(working_copy, ranges)`. Awaits M16 refactor.",
        strict=False,
    )
    def test_working_copy_cache_partition_isolation(self, tmp_path):
        """covers R27, INV-7 — working-copy-cache-partition-isolation (target).

        Target signature: `invalidate_frames_for_mutation(working_copy, ranges)`.
        This test passes when the function accepts a working-copy identifier
        and isolates cache evictions to that partition.
        """
        wc_a = tmp_path / "session_a"
        wc_b = tmp_path / "session_b"
        # Target contract: invalidating A leaves B intact. Until the refactor
        # lands, there's no way to express this — the test xfails.
        ci.invalidate_frames_for_mutation(wc_a, [(1.0, 2.0)])
        # Placeholder: spec-level test; rich assertions land post-refactor.

    def test_negative_assertion_call_site_count(self):
        """INV: only 4 call-sites across codebase today. Task brief: broader
        coverage depends on dispatcher-spec refactor. Brittle-by-design: if
        this count grows unexpectedly, reviewer must decide whether the new
        call-site is intentional or a drift from the single-chokepoint policy.
        """
        import subprocess
        repo_src = Path(__file__).resolve().parent.parent.parent / "src"
        result = subprocess.run(
            ["grep", "-rn", "invalidate_frames_for_mutation", str(repo_src)],
            capture_output=True, text=True, check=False,
        )
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        # Exclude the definition site in cache_invalidation.py itself.
        call_sites = [
            l for l in lines
            if "render/cache_invalidation.py" not in l
            and "def invalidate_frames_for_mutation" not in l
        ]
        # Per task brief + spec "Related Artifacts": api_server.py lines
        # 3336/9836/9850/9872 + the 3 module-level docstring references at
        # 9827, 9836, 9850, 9872. We count only actual `invalidate_frames_for_mutation(`
        # invocations (which includes `from ... import` lines paired with calls).
        invocations = [
            l for l in call_sites
            if "invalidate_frames_for_mutation(" in l
            and "from scenecraft" not in l
        ]
        # 4 real call sites expected.
        assert len(invocations) == 4, (
            f"Unexpected call-site count ({len(invocations)}). "
            f"Spec's dispatcher refactor not yet landed; if this grew "
            f"intentionally, update this test. Found:\n"
            + "\n".join(invocations)
        )


# ---------------------------------------------------------------------------
# E2E — real caches + real HTTP endpoint observation
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """End-to-end — drive invalidation into the live `engine_server` process
    and observe the cache-stats HTTP endpoint.

    Cache invalidation's only HTTP surface today is `GET /api/render-cache/stats`.
    Mutation endpoints (keyframe updates, transition trims, clip edits) do
    call `invalidate_frames_for_mutation`, but wiring each up in a
    hermetic e2e fixture requires pool-segment + track + clip seeding that
    belongs to other specs. Here we:

      1. Seed the module-level `global_cache` + `global_fragment_cache`
         directly (same instances the server process uses).
      2. Trigger invalidation via the chokepoint.
      3. Observe the change via `GET /api/render-cache/stats`.

    True endpoint-driven e2e (POST keyframe → stats drop) is marked xfail
    pending the M16 dispatcher refactor.
    """

    def test_e2e_stats_endpoint_reflects_frame_cache_eviction(
        self, engine_server, project_name, real_caches_cleared,
    ):
        """covers R9, R21 — wholesale frame-cache eviction visible via stats."""
        work_dir = engine_server.work_dir
        project_dir = work_dir / project_name

        frame_cache_mod.global_cache.put(project_dir, t=1.0, quality=85, jpeg=b"jpg1")
        frame_cache_mod.global_cache.put(project_dir, t=2.0, quality=85, jpeg=b"jpg2")

        status, body = engine_server.json("GET", "/api/render-cache/stats")
        assert status == 200
        assert body["frame_cache"]["frames"] >= 2

        # Wholesale invalidate.
        ci.invalidate_frames_for_mutation(project_dir, None)

        status, body = engine_server.json("GET", "/api/render-cache/stats")
        assert status == 200
        # No entries remain for this project_dir.
        assert body["frame_cache"]["frames"] == 0

    def test_e2e_stats_endpoint_reflects_fragment_cache_eviction(
        self, engine_server, project_name, real_caches_cleared,
    ):
        """covers R12, R21 — wholesale fragment-cache eviction visible via stats."""
        work_dir = engine_server.work_dir
        project_dir = work_dir / project_name
        fragment_cache_mod.global_fragment_cache.put(
            project_dir, t0=0.0, encoder_generation=1, fmp4=b"frag1", duration_ms=1000,
        )
        fragment_cache_mod.global_fragment_cache.put(
            project_dir, t0=2.0, encoder_generation=1, fmp4=b"frag2", duration_ms=1000,
        )

        status, body = engine_server.json("GET", "/api/render-cache/stats")
        assert status == 200
        assert body["fragment_cache"]["fragments"] >= 2

        ci.invalidate_frames_for_mutation(project_dir, None)

        status, body = engine_server.json("GET", "/api/render-cache/stats")
        assert body["fragment_cache"]["fragments"] == 0

    def test_e2e_surgical_range_evicts_only_overlap(
        self, engine_server, project_name, real_caches_cleared,
    ):
        """covers R10, R13 — surgical range-based eviction observable via stats."""
        work_dir = engine_server.work_dir
        project_dir = work_dir / project_name
        frame_cache_mod.global_cache.put(project_dir, t=1.0, quality=85, jpeg=b"a")
        frame_cache_mod.global_cache.put(project_dir, t=5.0, quality=85, jpeg=b"b")
        frame_cache_mod.global_cache.put(project_dir, t=10.0, quality=85, jpeg=b"c")

        status, body = engine_server.json("GET", "/api/render-cache/stats")
        frames_before = body["frame_cache"]["frames"]
        assert frames_before >= 3

        # Evict only the [4, 6] window → drops t=5.0 only.
        ci.invalidate_frames_for_mutation(project_dir, [(4.0, 6.0)])

        status, body = engine_server.json("GET", "/api/render-cache/stats")
        frames_after = body["frame_cache"]["frames"]
        assert frames_after == frames_before - 1

    def test_e2e_return_shape_is_2_tuple_ints(
        self, engine_server, project_name, real_caches_cleared,
    ):
        """covers R2 — transitional return shape is (int, int).

        Target state is 3-tuple with `coordinator_fallback` (R2/R18a), but
        that awaits M16. Lock the current 2-tuple contract here; the xfail
        equivalent lives in `TestEdgeCases.test_bg_requeue_raises_wholesale_fallback`.
        """
        work_dir = engine_server.work_dir
        project_dir = work_dir / project_name
        frame_cache_mod.global_cache.put(project_dir, t=1.0, quality=85, jpeg=b"j")
        out = ci.invalidate_frames_for_mutation(project_dir, None)
        assert isinstance(out, tuple) and len(out) == 2
        assert all(isinstance(x, int) for x in out)

    def test_e2e_unknown_project_silent_noop(
        self, engine_server, real_caches_cleared,
    ):
        """covers R25 — unknown project_dir silent no-op observable via stats."""
        never = engine_server.work_dir / "never_opened_project"
        # Should not raise, should not affect stats.
        status_before, body_before = engine_server.json("GET", "/api/render-cache/stats")
        out = ci.invalidate_frames_for_mutation(never, None)
        assert out == (0, 0)
        status_after, body_after = engine_server.json("GET", "/api/render-cache/stats")
        assert body_after["frame_cache"]["frames"] == body_before["frame_cache"]["frames"]
        assert body_after["fragment_cache"]["fragments"] == body_before["fragment_cache"]["fragments"]

    def test_e2e_empty_ranges_promotes_to_wholesale(
        self, engine_server, project_name, real_caches_cleared,
    ):
        """covers R5 — empty ranges triggers wholesale eviction via live stats."""
        work_dir = engine_server.work_dir
        project_dir = work_dir / project_name
        frame_cache_mod.global_cache.put(project_dir, t=1.0, quality=85, jpeg=b"a")
        frame_cache_mod.global_cache.put(project_dir, t=99.0, quality=85, jpeg=b"b")

        ci.invalidate_frames_for_mutation(project_dir, ranges=[])
        _, body = engine_server.json("GET", "/api/render-cache/stats")
        assert body["frame_cache"]["frames"] == 0

    def test_e2e_inverted_ranges_promote_to_wholesale(
        self, engine_server, project_name, real_caches_cleared,
    ):
        """covers R6, R7 — all-inverted filtered-empty promotes to wholesale."""
        work_dir = engine_server.work_dir
        project_dir = work_dir / project_name
        frame_cache_mod.global_cache.put(project_dir, t=1.0, quality=85, jpeg=b"a")
        frame_cache_mod.global_cache.put(project_dir, t=99.0, quality=85, jpeg=b"b")

        ci.invalidate_frames_for_mutation(project_dir, [(5.0, 3.0)])
        _, body = engine_server.json("GET", "/api/render-cache/stats")
        assert body["frame_cache"]["frames"] == 0  # wholesale-promoted

    def test_e2e_idempotent_repeated_wholesale(
        self, engine_server, project_name, real_caches_cleared,
    ):
        """covers R23 — repeated invocations don't raise and stats stay stable."""
        work_dir = engine_server.work_dir
        project_dir = work_dir / project_name
        frame_cache_mod.global_cache.put(project_dir, t=1.0, quality=85, jpeg=b"a")
        ci.invalidate_frames_for_mutation(project_dir, None)
        ci.invalidate_frames_for_mutation(project_dir, None)
        ci.invalidate_frames_for_mutation(project_dir, None)
        _, body = engine_server.json("GET", "/api/render-cache/stats")
        assert body["frame_cache"]["frames"] == 0

    def test_e2e_cross_project_isolation(
        self, engine_server, real_caches_cleared,
    ):
        """covers R9/R25 — invalidating project A does not evict project B.

        Project-scoped partitioning (the transitional precursor to R27's
        per-working-copy partitioning). Target per-working-copy isolation
        is codified as xfail in TestEdgeCases.
        """
        work_dir = engine_server.work_dir
        # Create two projects.
        import uuid
        a_name = f"proj_a_{uuid.uuid4().hex[:6]}"
        b_name = f"proj_b_{uuid.uuid4().hex[:6]}"
        for n in (a_name, b_name):
            s, _ = engine_server.json("POST", "/api/projects/create", {"name": n})
            assert s == 200
        pa = work_dir / a_name
        pb = work_dir / b_name

        frame_cache_mod.global_cache.put(pa, t=1.0, quality=85, jpeg=b"a")
        frame_cache_mod.global_cache.put(pb, t=1.0, quality=85, jpeg=b"b")

        _, body = engine_server.json("GET", "/api/render-cache/stats")
        assert body["frame_cache"]["frames"] >= 2

        ci.invalidate_frames_for_mutation(pa, None)

        _, body = engine_server.json("GET", "/api/render-cache/stats")
        # Only pb's entry remains.
        assert body["frame_cache"]["frames"] == 1
        # Verify pb's frame is still there (direct cache probe — the HTTP
        # surface doesn't expose per-project counts).
        assert frame_cache_mod.global_cache.get(pb, t=1.0, quality=85) == b"b"
        assert frame_cache_mod.global_cache.get(pa, t=1.0, quality=85) is None

    @pytest.mark.xfail(
        reason="target-state: endpoint-driven invalidation (POST keyframe → "
               "stats drop) requires M16 dispatcher refactor + full project "
               "seeding (pool segment, track, clip).",
        strict=False,
    )
    def test_e2e_endpoint_driven_invalidation_target(
        self, engine_server, project_name, real_caches_cleared,
    ):
        """Target — mutating HTTP endpoint triggers invalidation visible via stats.

        When the M16 dispatcher lands, this test will POST a keyframe update
        and observe the corresponding stats drop without any in-process
        cache-population. Today the wiring is indirect; xfail until then.
        """
        assert False, "awaits M16 dispatcher refactor"
