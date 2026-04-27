# Task 79: Engine Providers (Typed + Legacy) Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-providers-typed-and-legacy`](../../specs/local.engine-providers-typed-and-legacy.md)
**Design Reference**: [`local.engine-providers-typed-and-legacy`](../../specs/local.engine-providers-typed-and-legacy.md)
**Estimated Time**: 6-8 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit tests for `local.engine-providers-typed-and-legacy.md`. Lock in the dual registration + dispatch contract for typed providers (new) and legacy dict-based providers (old). Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

The provider registry is mid-migration: typed providers (new) sit alongside legacy dict-based providers (old). The refactor can't afford to drop either. This spec locks the registration + dispatch contract for both. Builds on task-70 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-providers-typed-and-legacy.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_providers_typed_and_legacy.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Typed provider registration — register a typed provider; assert it's discoverable by capability.
- Legacy provider registration — same, via the dict-based API.
- Dispatch parity — call via both paths with matching inputs; assert identical outputs.
- Capability query — request a capability that only one type provides; assert correct routing.
- Conflict resolution — register a typed + legacy provider for the same capability; assert spec-defined precedence.
- Error propagation — provider raises; assert legacy envelope shape is preserved.

Target-ideal behaviors (e.g., legacy deprecation warnings, capability-based overrides) → `xfail`.

### 4. Cover every Behavior Table row

### 5. No e2e section (direct-call surface)

```python
# NOTE: no e2e — local.engine-providers-typed-and-legacy.md is an in-process registry; HTTP dispatch via providers is covered by task-80 (generation-pipelines) and task-83 (chat-pipeline).
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_providers_typed_and_legacy.py -v
git add tests/specs/test_engine_providers_typed_and_legacy.py
git commit -m "test(M18-79): engine-providers-typed-and-legacy regression tests — <N> unit"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] `# NOTE: no e2e` comment at bottom
- [ ] `pytest ... -v` passes

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Parity test typed-vs-legacy | Yes | Migration safety net — they must produce equal results. |
| Conflict precedence tested explicitly | Yes | Where bugs live. |

---

## Notes

- Stub providers should be minimal — the registry is the focus, not the provider logic.
- If the legacy API is being sunset per spec, xfail the parity test with `reason="legacy deprecated; ticket <N>"`.
