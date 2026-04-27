# Milestone 18: Engine Regression Test Suite

**Goal**: Write unit + e2e regression tests against all 18 engine specs (one test file per spec, vertically per-domain) to lock in current engine behavior and target-ideal contract ahead of the imminent M16 FastAPI refactor. The suite is the safety net: every refactor PR must keep this suite green (modulo expected `xfail` → pass flips, which become visible milestone signals).
**Duration**: ~4 weeks (18 tasks, ~100 dev hours; ~25h/week focus time)
**Dependencies**: None blocking. The 18 engine specs were committed 2026-04-27 and cover the full engine surface (DB connection + schema, migrations, plugin loading, MCP bridge, cache invalidation, providers, generation, render, analysis, chat, file-serving, bootstrap, CLI, REST dispatcher). Some tasks will surface engine bugs — those get filed as M16 refactor-scope items or immediate fixes and do not block M18.
**Status**: Not Started

---

## Overview

The engine has a hand-rolled dispatcher (`api_server.py`), a thread-memoized SQLite pool, a plugin-host with sidecar tables, a provider registry straddling a typed / legacy split, a render pipeline, an analysis-cache layer, and a chat pipeline — all sharing one DB. M16 will replace `api_server.py` with FastAPI. Without a regression suite, that refactor is a leap of faith.

M18 turns each of the 18 engine specs into a runnable contract. Per-spec:

- **One test file** at `tests/specs/test_engine_<slug>.py`
- **Every requirement** `Rn` in the spec maps to ≥1 pytest function, docstring annotated `"""covers R1, R3, OQ-2"""`
- **Every `Behavior Table` row** is either a currently-passing test (transitional behavior) or an `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)` test (target-ideal behavior). No silent omissions.
- **E2E tests** live at the bottom of the same file under `class TestEndToEnd:` or a `# === E2E ===` divider, exercising the HTTP/WS surface when the spec covers it. DB-only specs (connection, schema, effects-curves, undo-redo, analysis-caches) may omit e2e with an explicit note.

**xfail, not skip**, is load-bearing — it communicates "this is the target; it will pass after the refactor" rather than "skipped, inspect later". When the M16 refactor PR flips `xfail` → pass, that's a visible milestone signal in CI.

---

## Deliverables

### 1. Test-spec directory

- `tests/specs/__init__.py`
- `tests/specs/conftest.py` — shared fixtures: in-memory SQLite test DB, temp `project_dir`, threading helpers, lightweight test-server boot for e2e
- `tests/specs/test_engine_<slug>.py` × 18 — one file per engine spec

### 2. Coverage guarantees

- Every spec requirement (`R1..Rn`) has ≥1 test with matching `(covers Rn)` docstring
- Every `Behavior Table` row has a corresponding test (transitional → passing; target-ideal → `xfail`)
- `pytest --collect-only tests/specs/test_engine_<slug>.py | wc -l` matches the spec's `### Base Cases` + `### Edge Cases` count
- E2E section present on every non-DB-only spec

### 3. Baseline green suite

- `pytest tests/specs/` exits 0 with: all expected-to-pass tests green; all target-state `xfail` tests yellow; no strict-xfail failures; no unexpected failures.
- Bugs surfaced during test authoring are filed as M16 scope items or fixed in the same PR at author discretion (small fixes) — not allowed to hide as `xfail` without a tracking reference.

---

## Tasks

Eighteen tasks total (T70–T87), ordered vertically per-domain with foundational DB specs first, then plugin/MCP/cache infra, then typed providers + pipelines, then HTTP/WS/chat/server/CLI/REST surfaces.

| # | Spec slug | Est hrs | Dependencies |
|---|---|---|---|
| 70 | engine-connection-and-transactions | 3–4 | None |
| 71 | engine-db-schema-core-entities | 3–4 | T70 |
| 72 | engine-db-effects-and-curves | 3–4 | T70, T71 |
| 73 | engine-db-undo-redo | 4–6 | T70, T71 |
| 74 | engine-db-analysis-caches | 4–6 | T70, T71 |
| 75 | engine-migrations-framework | 4–6 | T70 |
| 76 | engine-plugin-loading-lifecycle | 4–6 | T70 |
| 77 | engine-mcp-bridge | 4–6 | T70, T76 |
| 78 | engine-cache-invalidation | 4–6 | T70 |
| 79 | engine-providers-typed-and-legacy | 6–8 | T70 |
| 80 | engine-generation-pipelines | 6–10 | T70, T79 |
| 81 | engine-render-pipeline | 6–10 | T70 |
| 82 | engine-analysis-handlers | 6–8 | T70, T74 |
| 83 | engine-chat-pipeline | 6–10 | T70, T79 |
| 84 | engine-file-serving-and-uploads | 6–8 | T70 |
| 85 | engine-server-bootstrap | 6–8 | T70 |
| 86 | engine-cli-admin-commands | 6–8 | T70 |
| 87 | engine-rest-api-dispatcher | 8–10 | T70, T84, T85 |

**Total estimated**: ~95–130 dev hours. Central point estimate: ~100h.

---

## Success Criteria

- [ ] `tests/specs/test_engine_*.py` exists for all 18 engine specs
- [ ] `pytest tests/specs/` exits 0
- [ ] Every spec requirement has ≥1 covering test
- [ ] Every Behavior Table row has a test (pass or xfail)
- [ ] Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`
- [ ] No silent omissions (every `undefined` row accounted for by a test or an explicit `# NOTE:` comment)
- [ ] E2E section present on every non-DB-only spec
- [ ] Bugs surfaced during authoring are filed or fixed (not silently `xfail`'d)
- [ ] When M16 lands, the number of `xfail` tests visibly drops — measurable refactor signal

---

## Non-Goals

- Refactoring the engine (that's M16)
- New engine features
- Frontend tests (separate project; no vitest yet)
- Load / perf testing (separate concern)
- Property-based / fuzz testing (could be follow-up)

---

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Test mode | (c) both target-ideal and transitional | Target tests use `xfail`; flipping to pass is visible refactor signal |
| Structure | (b) one file per spec | Bidirectional traceability — refactor PR diff shows which spec's tests moved |
| xfail vs skip | xfail | Communicates "will pass after refactor" rather than "inspect later" |
| strict on xfail | `strict=False` | A target-state test that unexpectedly passes today is a gift, not a failure — M18 wants to discover those |
| E2E placement | same file as unit, under `class TestEndToEnd:` | Split only when file > ~800 LOC |
| E2E transport | `httpx` + `TestClient` (if FastAPI is up) or stdlib client against running legacy server | Spec-agnostic; pick per-task based on spec's HTTP/WS touchpoints |
| DB-only specs | T70–T74 may have no e2e; note explicitly | Connection/schema/effects/undo/caches are pure DB layer |

---

## Related Artifacts

- `agent/specs/local.engine-*.md` — 18 specs (committed 2026-04-27)
- `agent/milestones/milestone-16-fastapi-migration-and-tool-codegen.md` — the refactor this suite protects
- `tests/` — existing 897-test suite; M18 adds `tests/specs/` as a parallel spec-locked tier
- `pyproject.toml` — `pytest>=7.0.0` already in `[dev]`

---

## Open Questions

- **OQ-1** — When a target-state test unexpectedly passes today (strict=False lets it through silently), should CI flag it for promotion? *Recommendation*: add a nightly job running with `strict=True`; failures mean a test should be re-classified as transitional. Defer until baseline is green.
- **OQ-2** — Should per-spec `conftest_<slug>.py` files be preferred over a single `tests/specs/conftest.py`? *Recommendation*: start flat (single `conftest.py`); split only if fixtures diverge. Revisit after T75.
- **OQ-3** — Shared e2e server boot: per-test or session-scoped? *Recommendation*: session-scoped for speed; per-test DB isolation via transaction rollback. Settle in T70 when the fixture is authored.
