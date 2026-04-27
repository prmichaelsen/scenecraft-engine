# Task 91: GET /api/projects/:name/settings must 404 on missing project

**Milestone**: None (unassigned; surfaced by M18 task-88 retroactive e2e)
**Design Reference**: [engine-rest-api-dispatcher R17/R18](../../specs/local.engine-rest-api-dispatcher.md)
**Estimated Time**: 0.5h
**Dependencies**: None
**Status**: Not Started
**Repositories**: `scenecraft-engine`

---

## Objective

Make `GET /api/projects/<name>/settings` return 404 when `<name>` doesn't resolve to an existing project directory, matching the project_dir-resolution contract (R17/R18) that every other project-scoped endpoint already honors.

---

## Context

Surfaced by M18 task-88 (retroactive e2e coverage for task-70 + task-71), commit `04aa3e9`. During e2e, the tester noticed that `GET /api/projects/<bogus>/settings` returns **200 with a default settings payload** rather than 404. Every other project-scoped endpoint (add-keyframe, etc.) correctly 404s on a missing project via the shared `_require_project_dir(project_name)` check.

### Bug Details

The settings GET handler silently constructs a default response when the project doesn't exist (likely returns "sensible defaults" for any project name). This masks legitimate missing-project errors — a typo in the URL returns plausible-looking data instead of a clear 404.

### Fix

Add `_require_project_dir(project_name)` (or equivalent) at the top of the settings GET handler, mirroring the pattern used in sibling handlers. Return 404 if the project doesn't exist.

---

## Steps

1. Open `src/scenecraft/api_server.py`; grep for the `settings` GET handler (look for a handler matching path `/api/projects/<project_name>/settings` with method GET).
2. Inspect a known-good sibling (e.g. an add-keyframe handler) to confirm the exact `_require_project_dir` pattern + the 404 response shape used project-wide.
3. Add the `_require_project_dir(project_name)` call as the first line of the settings GET handler. Return 404 with the standard error envelope on miss.
4. Add a regression test in the appropriate M18 test file (likely `tests/specs/test_engine_rest_api_dispatcher.py` per the design reference, or wherever settings endpoints are covered):
   - `GET /api/projects/<bogus>/settings` returns 404
   - `GET /api/projects/<real>/settings` still returns 200 with the expected payload
5. Run the relevant test module to confirm green:
   ```
   pytest tests/specs/test_engine_rest_api_dispatcher.py -v
   ```
6. Commit: `fix(api): return 404 on GET /settings when project doesn't exist`.

---

## Verification Checklist

- [ ] `GET /api/projects/<bogus>/settings` returns 404 with standard error envelope
- [ ] `GET /api/projects/<real>/settings` still returns 200 with settings payload (no regression)
- [ ] Regression test added
- [ ] Pattern matches sibling handlers' `_require_project_dir` usage exactly

---

## Key Design Decisions

- **Use existing `_require_project_dir` helper, not a new one.** Consistency with every other project-scoped endpoint; a new helper would fragment the contract.
- **404, not 400.** Matches sibling handlers and REST convention: the resource (project) is not found.
- **No witness xfail to remove.** Bug was caught by human inspection during e2e, not by an xfailed test; the regression test is net-new.
