# Task 94: isolate_vocals plugin imports scenecraft.db — violates R9a / R39

**Milestone**: None (unassigned; surfaced by M18 task-80 regression tests)
**Design Reference**: [engine-generation-pipelines R39](../../specs/local.engine-generation-pipelines.md), audit-2 R9a (plugin boundary)
**Estimated Time**: 2h
**Dependencies**: None
**Status**: Not Started
**Repositories**: `scenecraft-engine`

---

## Objective

Remove direct `from scenecraft.db import …` calls from
`src/scenecraft/plugins/isolate_vocals/isolate_vocals.py` so the plugin
respects the audit-2 R9a / R39 boundary contract (plugins MUST NOT import
the engine's DB module directly; they go through `plugin_api`).

---

## Context

Surfaced by M18 task-80 (`engine-generation-pipelines`) regression tests
while writing `test_no_plugin_db_import` for R39. The test is currently
xfailed against this exact violation; flips green when this task lands.

### Bug Details

`src/scenecraft/plugins/isolate_vocals/isolate_vocals.py` contains:

- Line 255: `from scenecraft.db import get_audio_clips`
- Line 371: `from scenecraft.db import _retry_on_locked, get_db`

R9a (audit-2) and R39 (engine-generation-pipelines spec) both forbid plugin
runtime code from importing `scenecraft.db`. The plugin must use
`plugin_api` surface methods. `get_db` and `_retry_on_locked` are
particularly egregious — they expose engine-internal connection management
to a plugin boundary.

### Fix Approach

1. Add the missing surface methods to `plugin_api` (e.g.
   `plugin_api.audio.get_audio_clips`, plus a sidecar-table write helper
   that wraps `_retry_on_locked` internally — plugins never see the
   connection or the retry helper directly).
2. Replace the direct imports in `isolate_vocals.py` with calls into
   `plugin_api`.
3. Flip `test_no_plugin_db_import` (in
   `tests/specs/test_engine_generation_pipelines.py::TestNarrativeLegacy`)
   from `@xfail` to a normal assertion.

Test files under `src/scenecraft/plugins/**/tests/` are intentionally
permitted to import `scenecraft.db` (they are infrastructure, not
runtime). The xfailed test already excludes them.

---

## Steps

1. Read `plugin_api` surface; identify whether it already exposes
   audio-clip read + sidecar-write helpers.
2. Add missing methods if absent, mirroring how other plugins access
   engine state.
3. Refactor the two import sites in `isolate_vocals.py`.
4. Re-run plugin tests — none should regress.
5. Remove `@xfail` from `test_no_plugin_db_import`.

---

## Verification

- [ ] `grep -rn "from scenecraft.db" src/scenecraft/plugins/ --include="*.py" | grep -v /tests/` returns nothing.
- [ ] Plugin tests still pass.
- [ ] `pytest tests/specs/test_engine_generation_pipelines.py::TestNarrativeLegacy::test_no_plugin_db_import` passes (no longer xfailing).
- [ ] No new direct DB imports introduced elsewhere.

---

## Notes

Surfaced 2026-04-27 while writing M18 task-80 regression tests. Tracked
as a known violation; the test asserts the negative state today via
`@xfail(strict=False)` so the flip lands automatically when this task
ships.
