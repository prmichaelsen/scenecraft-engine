# Task 88: Retroactive e2e coverage for task-70 + task-71

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-connection-and-transactions`](../../specs/local.engine-connection-and-transactions.md) + [`local.engine-db-schema-core-entities`](../../specs/local.engine-db-schema-core-entities.md)
**Design Reference**: both underlying specs (`engine-connection-and-transactions.md` + `engine-db-schema-core-entities.md`)
**Estimated Time**: 8 hours
**Dependencies**: task-70 (done), task-71 (in progress)
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Add **comprehensive e2e coverage** to `tests/specs/test_engine_connection_and_transactions.py` + `tests/specs/test_engine_db_schema_core_entities.py`. Task-70 was briefed with "no e2e — internal DAL"; task-71 likewise. **This is reversed**: every requirement's observable effect must have an HTTP/WS-level test. E2E MUST exercise the live HTTP/WS surface, not dispatcher-level smoke tests.

---

## Context

Original task-70/71 briefs said "no e2e for DAL specs" — the user reversed that decision mid-flight when it became clear the M16 FastAPI refactor can silently break DAL ↔ handler boundaries if we only test DAL at module level and HTTP at dispatcher level with no bridge coverage.

The regression-safety argument: if DAL tests pass and dispatcher tests pass, but the transport → DAL bridge silently drops a kwarg, a transaction boundary, or a connection-pool guarantee, neither existing tier catches it. The bridge must be exercised end-to-end for every requirement that has an observable effect through HTTP or WS.

---

## Steps

### 1. Flesh out the `engine_server` fixture

Task-70 seeded `tests/specs/conftest.py::engine_server` as a stub. Flesh it out here:

- Boot a test engine instance against a temp `work_dir` via `scenecraft.api_server.run_server` in a background thread.
- Session-scoped; per-test DB isolation via the project-dir fixture (each test gets its own temp project).
- Yields an `httpx.Client` bound to the server's base URL, plus a `ws_url` attribute for websocket tests.
- Teardown: signal shutdown, join the thread with a 5s timeout.

### 2. Task-70 spec e2e — `test_engine_connection_and_transactions.py`

Add a `# === E2E ===` / `class TestEndToEnd:` section with tests covering the **live HTTP surface** for each requirement:

- **Connection memoization via HTTP**: fire 2 requests on the same server thread; patch `sqlite3.connect` to count; assert exactly 1 call.
- **Retry-on-locked under concurrent write pressure**: fire N concurrent `POST /api/projects/:name/keyframes` requests against a project with an artificially held write lock; assert no 5xx, all eventually 2xx.
- **Transaction rollback via HTTP error path**: POST an endpoint that internally raises mid-transaction; assert response is a legible error envelope AND the DB row is NOT present (rollback occurred across the HTTP boundary).
- **PRAGMA effects observable via concurrency**: fire a long-running read (e.g., GET /render-frame on a large timeline) concurrent with a write (POST a keyframe); assert both complete (WAL mode lets reads proceed during writes).
- **Busy-timeout observable**: construct a deliberate contention; assert wall-time bounded by busy_timeout, not indefinite.
- **Per-project connection isolation**: interleave requests against two different projects; assert no cross-project transaction visibility.
- **Connection pool survives WS lifecycle**: open a WS, make a REST request, close WS, make another REST request; assert the connection is still memoized.

Target: **~25 e2e tests** covering task-70's domain.

### 3. Task-71 spec e2e — `test_engine_db_schema_core_entities.py`

Add a `# === E2E ===` / `class TestEndToEnd:` section with tests covering every requirement of `engine-db-schema-core-entities.md` through HTTP. Map Behavior Table rows to endpoints:

- **Keyframes**:
  - `POST /api/projects/:name/keyframes` + `GET` roundtrip (R1, R2, row #1)
  - `DELETE .../keyframes/:id` + subsequent `GET` verifies soft-delete (R4, row #2)
  - `POST .../keyframes/:id/restore` (R4, row #3)
  - `PATCH .../keyframes/:id` with new timestamp → `GET .../audio-clips` shows shifted start/end (R5, row #5)
  - Zero-delta PATCH → clip unchanged (R5, row #6)
- **Transitions**:
  - `POST .../transitions` deriving track_id from from_kf (R11, row #7)
  - `DELETE .../transitions/:id` → linked audio_clips soft-deleted, links gone (R10, row #8)
  - `POST .../transitions/:id/restore` → partial restore (R10, row #9)
  - Single-slot `selected` flatten observable via `GET` response shape (R12, row #11)
  - Dangling from_kf allowed via HTTP POST (R7, row #42)
- **Transition effects**:
  - `POST .../transitions/:id/effects` z_order auto-increment (R15, row #12)
  - `DELETE .../effects/:id` hard-delete (R16, row #13)
  - Effects persist on transition soft-delete via GET (row #14)
- **Audio tracks**:
  - `POST .../audio-tracks` → `GET` ordered by display_order (R17, R20, row #16)
  - `POST .../audio-tracks/reorder` sequential (R18, row #15)
  - `DELETE .../audio-tracks/:id` cascades to clips via subsequent GET (R19, row #17)
- **Audio clips**:
  - `POST .../audio-clips` + `GET` derived fields (playback_rate, effective_source_offset, linked_transition_id, variant_kind) (R25, rows #18, #19, #20)
  - `DELETE .../audio-clips/:id` soft-delete (R24, row #21)
  - `PATCH` with JSON remap + volume_curve coercion (R26)
- **Audio candidates**:
  - `POST .../audio-clips/:id/candidates` idempotent (R29, row #24)
  - `GET` ordered DESC (R30, row #25)
  - `POST .../audio-clips/:id/assign-candidate` with null reverts path (R31, row #26)
  - `DELETE .../audio-candidates/:clip/:seg` clears selection (R32, row #27)
  - **xfail**: `POST` for deleted clip → 400 `AUDIO_CLIP_DELETED` envelope (R52, row #37, OQ-3)
  - Bad source → 400 with canonical envelope (R29, row #34)
- **Tr candidates**:
  - `POST .../transitions/:id/candidates` idempotent (R35, row #22)
  - `GET` ordered ASC (R36, row #23)
  - `POST .../transitions/:source/clone-candidates/:target` count + preservation (R37, row #28)
  - **xfail**: `POST` for deleted transition → 400 `TRANSITION_DELETED` (R53, row #38, OQ-4)
  - Bad source → 400 (row #33)
- **Audio clip links**:
  - `POST .../audio-clip-links` upsert (R39, row #29)
  - `DELETE .../audio-clip-links/transition/:id` returns ids (R40, row #30)
- **Sections**:
  - `PUT .../sections` full replace (R43, row #31)
  - `GET .../sections` ordered (R42, row #32)
- **Target-state xfails** through HTTP surface (OQ-1/3/4/6/7/8):
  - `DELETE .../keyframes/:id?hard=true` with live transition → 400 `KEYFRAME_IN_USE` (R51, row #35)
  - Non-monotonic curve via PATCH → 400 `VALUE_ERROR` (R54, row #40)
  - Negative `remap.target_duration` via PATCH → 400 CHECK (R55, row #41)
  - Legacy NOT NULL track_id migration rebuild observable via boot-then-GET (R_transitional, row #43)
- **FK gap witnesses** through HTTP (orphan inserts succeed, documenting the gap):
  - `POST .../audio-candidates` with missing clip_id → 2xx today (row #44)

Target: **~40 e2e tests** covering task-71's domain.

### 4. Annotation convention

Each e2e test:

```python
def test_post_keyframe_roundtrip(self, engine_server, project_name):
    """covers R1, R2, row #1 (e2e)"""
    ...
```

Every e2e test MUST annotate `(covers Rn[, OQ-M], row #N)` matching the spec.

### 5. Xfail target-state tests

```python
@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
def test_post_audio_candidate_on_deleted_clip_rejected(self, engine_server, project_name):
    """covers R52, OQ-3, row #37 (e2e)"""
    ...
```

When the refactor lands, these xfails flip to pass — visible milestone signal.

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_connection_and_transactions.py tests/specs/test_engine_db_schema_core_entities.py -v --no-header
```

Assert: both unit + e2e present; xfails hold (no unexpected pass); no FAIL.

```bash
git add tests/specs/test_engine_connection_and_transactions.py \
        tests/specs/test_engine_db_schema_core_entities.py \
        tests/specs/conftest.py
git commit -m "test(M18-88): retroactive e2e for task-70 + task-71 — <M> e2e tests covering connection + core-entities DAL surfaces"
```

---

## Verification

- [ ] `tests/specs/conftest.py::engine_server` fixture fully functional (boots, teardown clean)
- [ ] ~25 e2e tests for task-70 domain
- [ ] ~40 e2e tests for task-71 domain
- [ ] Every spec requirement with an HTTP/WS-observable effect has ≥1 e2e test
- [ ] Every Behavior Table row in both specs has an e2e test or explicit `# NOTE:` exclusion
- [ ] Target-state (OQ-1/3/4/6/7/8) tests are `xfail(strict=False)` at the HTTP level
- [ ] `pytest tests/specs/test_engine_connection_and_transactions.py tests/specs/test_engine_db_schema_core_entities.py -v --no-header` shows unit + e2e; xfails hold; no FAIL

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| E2E via live HTTP, not dispatcher | Yes | Dispatcher-level smoke tests miss transport ↔ DAL drift during M16 refactor |
| Xfails mirror unit-level xfails | Yes | Target-state tests must flip at both tiers when the refactor lands |
| Session-scoped engine_server | Yes | Boot cost is non-trivial; per-test isolation via temp project_dir |
| WS coverage where observable | Yes | WS broadcast semantics are part of the public contract |

---

## Notes

- Do NOT modify task-70 (done) or task-71 (in progress) task docs. This task supplements them.
- If any e2e reveals a bug in current-state behavior, file it as an M16-scope item — don't silently xfail.
- The engine_server fixture landed here is the blueprint for tasks 75, 76, 77, 78, 80–87. Keep it clean.
