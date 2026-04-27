# Task 92: Fix update-timestamp handler body key inconsistency (newTimestamp vs timestamp)

**Milestone**: None (unassigned; surfaced by M18 task-88 retroactive e2e)
**Design Reference**: [engine-rest-api-dispatcher](../../specs/local.engine-rest-api-dispatcher.md)
**Estimated Time**: 0.5h
**Dependencies**: None
**Status**: Not Started
**Repositories**: `scenecraft-engine`

---

## Objective

Resolve the undocumented body-key mismatch in the `update-timestamp` handler: accept both `timestamp` (project-wide convention) and `newTimestamp` (current handler-specific requirement), documenting `timestamp` as canonical and `newTimestamp` as a legacy alias.

---

## Context

Surfaced by M18 task-88 (retroactive e2e coverage) report notes, commit `04aa3e9`. The e2e tester found that `POST .../update-timestamp` requires body key `newTimestamp`, while most other timestamp-related handlers and the frontend convention use plain `timestamp`. The mismatch is undocumented and breaks the principle of least surprise for callers.

### Bug Details

- Handler: `POST /api/projects/:name/.../update-timestamp` (grep for `update-timestamp` / `newTimestamp` in `api_server.py`).
- Current behavior: reads `body["newTimestamp"]`; fails (400 or KeyError) if the caller sends `timestamp`.
- Most other handlers: accept `timestamp`.

### Fix Approach

Two options considered:

- **(a)** Accept both: read `body.get("newTimestamp") or body.get("timestamp")`, preferring `newTimestamp` if both are present (preserves any in-flight caller that happens to pass both).
- **(b)** Standardize on `timestamp`, update any frontend callers that use `newTimestamp`.

**Choice: (a).** Lower-risk. (b) is cleaner long-term but requires a cross-repo coordinated change; (a) ships today, removes the foot-gun, and can be narrowed to `timestamp`-only in a follow-up once frontend callers are audited.

---

## Steps

1. Open `src/scenecraft/api_server.py`; grep for `update-timestamp` and `newTimestamp` to locate the handler.
2. Replace the `body["newTimestamp"]` read with something like:
   ```python
   ts = body.get("newTimestamp")
   if ts is None:
       ts = body.get("timestamp")
   if ts is None:
       return error_response(400, "missing 'timestamp' in request body")
   ```
   Prefer `newTimestamp` when both are present to preserve any caller that sends both.
3. Add regression tests covering:
   - Body `{"timestamp": X}` → handler succeeds
   - Body `{"newTimestamp": X}` → handler succeeds (legacy alias)
   - Body with both → uses `newTimestamp` value
   - Body with neither → 400
4. Update the `engine-rest-api-dispatcher` spec (if the endpoint is documented there) to note: `timestamp` is canonical; `newTimestamp` is a deprecated legacy alias. If not documented, add a brief paragraph.
5. Run the relevant test module:
   ```
   pytest tests/specs/test_engine_rest_api_dispatcher.py -v
   ```
6. Commit: `fix(api): accept both 'timestamp' and 'newTimestamp' in update-timestamp handler`.

---

## Verification Checklist

- [ ] Handler accepts `timestamp` (canonical)
- [ ] Handler still accepts `newTimestamp` (legacy alias)
- [ ] Handler prefers `newTimestamp` when both are present
- [ ] Handler returns 400 when neither is present
- [ ] Regression tests cover all four cases
- [ ] Spec updated to document canonical vs legacy alias (or added if not yet documented)

---

## Key Design Decisions

- **Accept-both (a) over standardize (b).** (a) is single-repo, non-breaking, and ships today. (b) requires coordinating a frontend change; deferred as a follow-up cleanup.
- **`newTimestamp` wins when both are present.** Preserves the behavior of any caller that accidentally sends both; the current value users are sending must continue to work verbatim.
- **`timestamp` is canonical going forward.** New callers should use `timestamp`; `newTimestamp` is documented as a legacy alias that may be removed once the frontend is audited.
