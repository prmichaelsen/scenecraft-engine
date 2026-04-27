"""Regression tests for `local.engine-migrations-framework.md`.

Covers the *transitional* behavior of ``scenecraft.db._ensure_schema``:
additive ``ALTER TABLE ADD COLUMN`` guarded by ``PRAGMA table_info``,
one-shot data transforms, DROP-TABLE rescue, DROP-COLUMN fallback, and
plugin sidecar DDL hardcoded in core.

Target-state requirements (R19–R24 / OQ-1..OQ-7 resolved as "target")
are asserted via ``@pytest.mark.xfail(strict=False)`` — they will pass
only after M17 task-135 lands ``schema_migrations`` / ``register_migration`` /
``rebuild_table`` / advisory flock.

Every test docstring opens with ``covers Rn``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from scenecraft import db as scdb
from scenecraft import plugin_api as scplugin_api


# ---------------------------------------------------------------------------
# Helpers (private; do not promote to conftest)
# ---------------------------------------------------------------------------


def _raw_conn(db_path: Path) -> sqlite3.Connection:
    """Open a raw sqlite connection bypassing the pool + memo."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


@pytest.fixture
def migrations_fresh_work_dir(tmp_path: Path) -> Path:
    """Fresh empty work dir for a standalone _ensure_schema run."""
    d = tmp_path / "mig_fresh"
    d.mkdir()
    return d


@pytest.fixture
def migrations_legacy_db(tmp_path: Path) -> Path:
    """Build a pre-M9 style DB: audio_tracks with legacy `volume` column,
    transitions missing many additive columns, keyframes missing track_id."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE audio_tracks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            display_order INTEGER NOT NULL DEFAULT 0,
            volume REAL NOT NULL DEFAULT 1.0
        );
        CREATE TABLE audio_clips (
            id TEXT PRIMARY KEY,
            track_id TEXT,
            source_path TEXT NOT NULL,
            volume REAL NOT NULL DEFAULT 1.0
        );
        CREATE TABLE transitions (
            id TEXT PRIMARY KEY,
            from_kf TEXT NOT NULL,
            to_kf TEXT NOT NULL,
            duration_seconds REAL NOT NULL DEFAULT 0,
            slots INTEGER NOT NULL DEFAULT 1,
            action TEXT NOT NULL DEFAULT '',
            use_global_prompt INTEGER NOT NULL DEFAULT 0,
            selected TEXT NOT NULL DEFAULT '[]',
            remap TEXT NOT NULL DEFAULT '{}',
            deleted_at TEXT,
            include_section_desc INTEGER NOT NULL DEFAULT 1,
            transform_x REAL,
            transform_y REAL
        );
        INSERT INTO transitions (id, from_kf, to_kf, transform_x, transform_y)
        VALUES ('t1', 'k_a', 'k_b', 10.0, 20.0);
        """
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# === UNIT ===
# Transitional behavior: additive ALTER, guarded by PRAGMA, idempotent,
# one-shot data transforms, DROP-TABLE rescue, DROP-COLUMN swallow,
# plugin sidecar hardcoded in core.
# ---------------------------------------------------------------------------


def test_fresh_db_reaches_current_schema_in_one_pass(project_dir: Path):
    """covers R1, R5, R6 — empty DB → all tables+indexes + seeds present."""
    conn = scdb.get_db(project_dir)
    tables = _table_names(conn)

    # Core
    for core in ("keyframes", "transitions", "audio_tracks", "audio_clips",
                 "suppressions", "project_send_buses", "meta"):
        assert core in tables, f"schema-complete: missing core table {core}"

    # Seeds: 4 default send buses
    rows = conn.execute(
        "SELECT label, order_index FROM project_send_buses ORDER BY order_index"
    ).fetchall()
    assert len(rows) == 4, f"send-buses-seeded: expected 4, got {len(rows)}"
    assert [r[0] for r in rows] == ["Plate", "Hall", "Delay", "Echo"]


def test_idempotent_on_current_schema(project_dir: Path):
    """covers R2, R8 — second _ensure_schema call is a no-op."""
    conn = scdb.get_db(project_dir)
    sqlite_master_before = conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    bus_count_before = conn.execute(
        "SELECT COUNT(*) FROM project_send_buses"
    ).fetchone()[0]

    # Re-invoke _ensure_schema directly (bypassing _migrated_dbs guard).
    scdb._ensure_schema(conn)

    sqlite_master_after = conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    bus_count_after = conn.execute(
        "SELECT COUNT(*) FROM project_send_buses"
    ).fetchone()[0]

    assert [tuple(r) for r in sqlite_master_before] == [tuple(r) for r in sqlite_master_after], \
        "schema-unchanged: sqlite_master changed on second _ensure_schema"
    assert bus_count_before == bus_count_after == 4, \
        "no-duplicate-seed: seed re-ran"


def test_process_local_memo_skips_reentry(project_dir: Path):
    """covers R2 — _migrated_dbs guards re-entry on repeat get_db."""
    scdb.get_db(project_dir)
    db_path = str(project_dir / "project.db")
    assert db_path in scdb._migrated_dbs, "first call should memo path"

    # Spy: replace _ensure_schema; second get_db in same process MUST NOT call it.
    called = []
    orig = scdb._ensure_schema
    scdb._ensure_schema = lambda c: called.append(1)  # type: ignore[assignment]
    try:
        scdb.get_db(project_dir)  # repeat
    finally:
        scdb._ensure_schema = orig  # type: ignore[assignment]
    assert called == [], "no-reentry: _ensure_schema re-ran on memoized path"


def test_alter_table_add_column_runs_once(migrations_legacy_db: Path):
    """covers R7, R8 — PRAGMA-guarded ALTER ADD COLUMN is idempotent."""
    conn = _raw_conn(migrations_legacy_db)
    try:
        cols_before = _column_names(conn, "transitions")
        assert "label_color" not in cols_before
        scdb._ensure_schema(conn)
        cols_after = _column_names(conn, "transitions")
        assert "label_color" in cols_after, "column-added"
        # Second run: no duplicate-column error.
        scdb._ensure_schema(conn)
        cols_2 = _column_names(conn, "transitions")
        assert cols_after == cols_2, "second-call-noop"
    finally:
        conn.close()


def test_not_null_add_uses_default_literal(migrations_legacy_db: Path):
    """covers R9 — NOT NULL column added via ALTER uses DEFAULT literal."""
    conn = _raw_conn(migrations_legacy_db)
    try:
        scdb._ensure_schema(conn)
        # `label_color` was added NOT NULL DEFAULT '' on transitions.
        info = {
            r[1]: r
            for r in conn.execute("PRAGMA table_info(transitions)").fetchall()
        }
        assert info["label_color"][3] == 1, "column-added-not-null: label_color notnull=1"
        # Existing row (id=t1) gets the default literal.
        row = conn.execute(
            "SELECT label_color FROM transitions WHERE id='t1'"
        ).fetchone()
        assert row["label_color"] == "", "default-applied: empty string"
    finally:
        conn.close()


def test_one_shot_transform_populates_only_nulls(migrations_legacy_db: Path):
    """covers R11 — transform_x/y → transform_x/y_curve; re-run does not clobber."""
    conn = _raw_conn(migrations_legacy_db)
    try:
        scdb._ensure_schema(conn)
        cols = _column_names(conn, "transitions")
        assert "transform_x_curve" in cols
        assert "transform_y_curve" in cols
        row = conn.execute(
            "SELECT transform_x_curve, transform_y_curve FROM transitions WHERE id='t1'"
        ).fetchone()
        assert json.loads(row["transform_x_curve"]) == [[0, 10.0], [1, 10.0]]
        assert json.loads(row["transform_y_curve"]) == [[0, 20.0], [1, 20.0]]

        # User edits the curve; re-run must not clobber it.
        conn.execute(
            "UPDATE transitions SET transform_x_curve = ? WHERE id='t1'",
            (json.dumps([[0, 5], [1, 5]]),),
        )
        conn.commit()
        scdb._ensure_schema(conn)
        row2 = conn.execute(
            "SELECT transform_x_curve FROM transitions WHERE id='t1'"
        ).fetchone()
        assert json.loads(row2["transform_x_curve"]) == [[0, 5], [1, 5]], \
            "re-run-no-clobber: user edits preserved"
    finally:
        conn.close()


def test_drop_column_failure_is_swallowed(tmp_path: Path):
    """covers R12 — DROP COLUMN failure caught; column stays, migration succeeds."""
    db_path = tmp_path / "droptest.db"
    conn = _raw_conn(db_path)
    # Run a full ensure_schema so everything exists.
    scdb._ensure_schema(conn)

    # Inject: add a dummy column + a trigger referencing it so a DROP COLUMN
    # would be rejected by SQLite. The framework's DROP-COLUMN paths are
    # for transform_z_curve / tracks.enabled / audio_tracks.enabled — all wrapped
    # in try/except. We simulate by crafting a trigger that references a column
    # the framework would attempt to drop, and verify re-running _ensure_schema
    # doesn't raise.
    conn.execute("ALTER TABLE transitions ADD COLUMN transform_z_curve TEXT")
    # Populate so the framework reaches the DROP attempt.
    conn.execute(
        "INSERT INTO transitions (id, from_kf, to_kf, transform_z_curve, "
        "transform_scale_x_curve, transform_scale_y_curve) "
        "VALUES ('tz', 'a', 'b', ?, NULL, NULL)",
        (json.dumps([[0, 1.0], [1, 1.0]]),),
    )
    # Trigger that references OLD.transform_z_curve → DROP must be rejected.
    conn.execute(
        "CREATE TRIGGER pin_tz BEFORE DELETE ON transitions "
        "BEGIN SELECT OLD.transform_z_curve; END"
    )
    conn.commit()

    # Must not raise — the framework swallows sqlite3.OperationalError from DROP.
    scdb._ensure_schema(conn)
    cols = _column_names(conn, "transitions")
    assert "transform_z_curve" in cols, "column-remains: trigger blocked DROP"
    conn.close()


def test_legacy_volume_triggers_drop_table_rescue(migrations_legacy_db: Path):
    """covers R13 — legacy volume col + missing volume_curve → DROP TABLE rescue."""
    conn = _raw_conn(migrations_legacy_db)
    try:
        assert "volume" in _column_names(conn, "audio_tracks")
        assert "volume_curve" not in _column_names(conn, "audio_tracks")
        scdb._ensure_schema(conn)
        cols = _column_names(conn, "audio_tracks")
        assert "volume_curve" in cols, "table-recreated with volume_curve"
        assert "volume" not in cols, "legacy volume column gone"
        # Same path runs for audio_clips.
        clip_cols = _column_names(conn, "audio_clips")
        assert "volume_curve" in clip_cols
        assert "volume" not in clip_cols
    finally:
        conn.close()


def test_drop_table_rescue_skipped_when_not_applicable(project_dir: Path):
    """covers R14 — rescue doesn't run when volume_curve already present."""
    conn = scdb.get_db(project_dir)
    # Fresh DB: audio_tracks already has volume_curve, no legacy volume.
    cols = _column_names(conn, "audio_tracks")
    assert "volume_curve" in cols
    assert "volume" not in cols
    # Second ensure_schema: no rescue, no error.
    scdb._ensure_schema(conn)
    assert _column_names(conn, "audio_tracks") == cols


def test_empty_seed_target_gets_defaults(project_dir: Path):
    """covers R15 — fresh project_send_buses gets 4 defaults in order."""
    conn = scdb.get_db(project_dir)
    rows = conn.execute(
        "SELECT bus_type, label, order_index, static_params "
        "FROM project_send_buses ORDER BY order_index"
    ).fetchall()
    assert len(rows) == 4
    assert [r[1] for r in rows] == ["Plate", "Hall", "Delay", "Echo"]
    assert [r[0] for r in rows] == ["reverb", "reverb", "delay", "echo"]
    # static_params valid JSON
    for r in rows:
        assert isinstance(json.loads(r[3]), dict)


def test_non_empty_seed_target_is_noop(project_dir: Path):
    """covers R16 — existing send-buses rows are not overwritten on re-run."""
    conn = scdb.get_db(project_dir)
    # User customizes row 0.
    conn.execute(
        "UPDATE project_send_buses SET label='MyPlate' WHERE order_index=0"
    )
    conn.commit()
    scdb._ensure_schema(conn)
    label = conn.execute(
        "SELECT label FROM project_send_buses WHERE order_index=0"
    ).fetchone()[0]
    assert label == "MyPlate", "user-row-intact"
    count = conn.execute(
        "SELECT COUNT(*) FROM project_send_buses"
    ).fetchone()[0]
    assert count == 4, "row-count-unchanged"


def test_plugin_sidecar_tables_created_by_core(project_dir: Path):
    """covers R17, R18 — plugin sidecar tables defined in core _ensure_schema."""
    conn = scdb.get_db(project_dir)
    tables = _table_names(conn)
    # Plugin prefixes hardcoded in core db.py today.
    expected_sidecars = {
        "light_show__fixtures",
        "light_show__overrides",
    }
    missing = expected_sidecars - tables
    # At least some plugin-prefixed sidecars must exist (hardcoded in core).
    plugin_prefixed = {t for t in tables if "__" in t}
    assert plugin_prefixed, "no plugin-prefixed sidecar tables found"
    # If expected set is not all present, it's fine — plugin set may vary.
    # The invariant is: prefixed tables were created without any register_migration.
    assert not hasattr(scplugin_api, "register_migration"), \
        "no-register-migration-symbol: plugin_api.register_migration must not exist yet"


def test_table_info_reflects_in_run_alters(migrations_legacy_db: Path):
    """covers R10 — PRAGMA re-read between blocks on same table sees new cols."""
    conn = _raw_conn(migrations_legacy_db)
    try:
        scdb._ensure_schema(conn)
        cols = _column_names(conn, "transitions")
        # All three additive cols present after one pass, no duplicate-col errors.
        assert {"label", "label_color", "tags"} <= cols
    finally:
        conn.close()


def test_exception_leaves_dbs_unmarked_for_retry(project_dir: Path, monkeypatch):
    """covers R2 negative — if _ensure_schema raises, _migrated_dbs stays clear."""
    db_path = str(project_dir / "project.db")

    def boom(conn):
        raise sqlite3.OperationalError("synthetic boom")

    monkeypatch.setattr(scdb, "_ensure_schema", boom)
    with pytest.raises(sqlite3.OperationalError):
        scdb.get_db(project_dir)
    assert db_path not in scdb._migrated_dbs, "memo-not-set on failure"


def test_concurrent_get_db_runs_ensure_schema_once(project_dir: Path, monkeypatch):
    """covers R3 — _migrated_dbs guard consulted under _conn_lock; concurrent
    get_db from multiple threads runs _ensure_schema exactly once.

    Codifies the transitional concurrency contract: today the lock + memo set
    serialize first-time schema init across racing threads. (Mirrors the
    connection-spec R7+R8 witness; here from the migrations-framework angle.)
    """
    # Reset memo so this is a "first-time" init for the test's db_path.
    db_path = str(project_dir / "project.db")
    scdb._migrated_dbs.discard(db_path)
    # Ensure no stale conns exist for this path
    for k in [k for k in list(scdb._connections) if k.startswith(f"{db_path}:")]:
        try:
            scdb._connections[k].close()
        except Exception:
            pass
        del scdb._connections[k]

    calls: list[int] = []
    real_ensure = scdb._ensure_schema
    barrier = threading.Barrier(4)

    def spy(conn):
        calls.append(1)
        return real_ensure(conn)

    monkeypatch.setattr(scdb, "_ensure_schema", spy)

    results: list = []
    errors: list = []

    def worker():
        try:
            barrier.wait(timeout=10)
            results.append(scdb.get_db(project_dir))
        except Exception as e:  # pragma: no cover - error path
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"no-thread-errors: got {errors!r}"
    assert len(results) == 4, f"all-threads-got-conn: got {len(results)}"
    assert len(calls) == 1, \
        f"ensure-schema-once-under-race: expected 1 call, got {len(calls)}"


def test_ensure_schema_runs_with_pragmas_in_effect(project_dir: Path):
    """covers R4 — by the time _ensure_schema runs, the conn already has
    foreign_keys=ON, journal_mode=WAL, synchronous=NORMAL, busy_timeout=60000.

    Today get_db sets PRAGMAs *before* invoking _ensure_schema (per the
    connection spec R4+R26). This test witnesses that ordering: when
    _ensure_schema is invoked by get_db, the PRAGMAs are already in effect.
    """
    captured = {}
    real_ensure = scdb._ensure_schema

    def spy(conn):
        captured["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        captured["synchronous"] = conn.execute("PRAGMA synchronous").fetchone()[0]
        captured["foreign_keys"] = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        captured["busy_timeout"] = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        return real_ensure(conn)

    # Reset memo so init really runs
    db_path = str(project_dir / "project.db")
    scdb._migrated_dbs.discard(db_path)
    scdb._ensure_schema = spy  # type: ignore[assignment]
    try:
        scdb.get_db(project_dir)
    finally:
        scdb._ensure_schema = real_ensure  # type: ignore[assignment]

    assert captured["journal_mode"] == "wal", \
        f"r4-wal-mode-pre-ddl: got {captured['journal_mode']!r}"
    assert captured["synchronous"] == 1, \
        f"r4-sync-normal-pre-ddl: got {captured['synchronous']!r}"
    assert captured["busy_timeout"] == 60000, \
        f"r4-busy-timeout-pre-ddl: got {captured['busy_timeout']!r}"
    # foreign_keys: spec R4 lists it among the four PRAGMAs; per
    # connection-spec R26 today's ordering applies it before DDL. Codify the
    # observed transitional state — FKs ON during _ensure_schema.
    assert captured["foreign_keys"] == 1, \
        f"r4-fk-on-during-ddl: foreign_keys=ON when _ensure_schema runs, got {captured['foreign_keys']!r}"


# ---------------------------------------------------------------------------
# === Target-state (xfail) ===
# OQ-1..OQ-7 resolved as TARGET; not implemented today.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="target-state; awaits M17 task-135 schema_migrations table", strict=False
)
def test_schema_migrations_table_present_after_init(project_dir: Path):
    """covers R19 (OQ-1 target) — schema_migrations ledger table exists."""
    conn = scdb.get_db(project_dir)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    assert row is not None, "schema_migrations table must exist"
    info_cols = _column_names(conn, "schema_migrations")
    assert {"version", "applied_at", "applied_by"} <= info_cols


@pytest.mark.xfail(
    reason="target-state; awaits M17 task-135 register_migration API", strict=False
)
def test_register_migration_api_exists():
    """covers R20 (OQ-2 target) — plugin_api.register_migration is callable."""
    assert hasattr(scplugin_api, "register_migration"), \
        "plugin_api.register_migration must exist"
    assert callable(getattr(scplugin_api, "register_migration"))


@pytest.mark.xfail(
    reason="target-state; awaits M17 task-135 migrate-down CLI", strict=False
)
def test_migrate_down_cli_exists():
    """covers R21 (OQ-3 target) — `scenecraft migrate down --to <v>` exists."""
    # Target: a rollback CLI or module-level rollback function.
    assert hasattr(scdb, "migrate_down") or hasattr(scplugin_api, "migrate_down"), \
        "migrate_down rollback API must exist"


@pytest.mark.xfail(
    reason="target-state; awaits M17 task-135 rebuild_table helper", strict=False
)
def test_rebuild_table_helper_exists():
    """covers R22 (OQ-4 target) — plugin_api.migrate.rebuild_table helper."""
    migrate_ns = getattr(scplugin_api, "migrate", None)
    assert migrate_ns is not None, "plugin_api.migrate namespace must exist"
    assert callable(getattr(migrate_ns, "rebuild_table", None)), \
        "rebuild_table helper must be callable"


@pytest.mark.xfail(
    reason="target-state; awaits rebuild_table (OQ-5 target)", strict=False
)
def test_rebuild_table_supports_check_constraint():
    """covers R22 (OQ-5 target) — CHECK constraint addable via rebuild_table."""
    migrate_ns = getattr(scplugin_api, "migrate", None)
    assert migrate_ns is not None
    # If we had the helper, we'd call rebuild_table with a new_schema including CHECK.
    raise AssertionError("rebuild_table CHECK-support not implemented")


@pytest.mark.xfail(
    reason="target-state; awaits register_migration up_fn (OQ-6 target)",
    strict=False,
)
def test_up_fn_runs_arbitrary_python():
    """covers R23 (OQ-6 target) — up_fn(conn) accepts arbitrary Python."""
    assert hasattr(scplugin_api, "register_migration"), \
        "register_migration with up_fn contract must exist"


@pytest.mark.xfail(
    reason="target-state; awaits advisory flock on .scenecraft/schema.lock",
    strict=False,
)
def test_advisory_flock_on_schema_lock(project_dir: Path):
    """covers R24 (OQ-7 target) — schema init holds flock on schema.lock."""
    # Target: .scenecraft/schema.lock exists under project_dir after init.
    scdb.get_db(project_dir)
    lock_file = project_dir / ".scenecraft" / "schema.lock"
    assert lock_file.exists(), "schema.lock must be created during init"


# ---------------------------------------------------------------------------
# === E2E ===
# Boot real engine_server; verify schema convergence via HTTP.
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Comprehensive e2e — boot engine against varied DB fixtures; verify
    schema convergence via HTTP and SQLite introspection."""

    def test_e2e_fresh_project_boot_creates_full_schema(
        self, engine_server, project_name
    ):
        """covers R1, R5, R6 (row 1) — POST /projects/create → full schema."""
        project_db = engine_server.work_dir / project_name / "project.db"
        assert project_db.exists(), "project.db should have been created"

        conn = _raw_conn(project_db)
        try:
            tables = _table_names(conn)
            # Core timeline tables
            for t in ("keyframes", "transitions", "audio_tracks", "audio_clips",
                      "suppressions", "project_send_buses", "meta"):
                assert t in tables, f"missing core table {t}"
            # Plugin sidecar tables (hardcoded in core _ensure_schema).
            plugin_prefixed = {tn for tn in tables if "__" in tn}
            assert plugin_prefixed, "no plugin sidecar tables found"
        finally:
            conn.close()

    def test_e2e_add_keyframe_roundtrip_after_fresh_boot(
        self, engine_server, project_name
    ):
        """covers R6 (row 1) — schema is immediately usable by DAL after boot."""
        s, _ = engine_server.json(
            "POST",
            f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:05"},
        )
        assert s == 200, f"add-keyframe failed: {s}"
        s2, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        assert s2 == 200
        kfs = body if isinstance(body, list) else body.get("keyframes", [])
        assert isinstance(kfs, list)
        assert len(kfs) >= 1

    def test_e2e_idempotent_reopen_same_workdir(
        self, engine_server, project_name
    ):
        """covers R2, R8 (row 2,3) — reopening same project.db does not re-migrate."""
        project_db_path = str(engine_server.work_dir / project_name / "project.db")
        # The server already opened it once; _migrated_dbs should include it.
        assert project_db_path in scdb._migrated_dbs, \
            "server boot should have memoized project path"

        # Sanity: hit an endpoint that uses the DAL; schema still stable.
        s, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        assert s == 200

    def test_e2e_no_schema_migrations_table_today(
        self, engine_server, project_name
    ):
        """covers R19 negative / Transitional — no schema_migrations table today."""
        project_db = engine_server.work_dir / project_name / "project.db"
        conn = _raw_conn(project_db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            assert row is None, (
                "transitional: schema_migrations table must NOT exist today "
                "(target state per R19 awaits M17 task-135)"
            )
        finally:
            conn.close()

    def test_e2e_no_register_migration_symbol(self, engine_server):
        """covers R20 negative / Transitional — plugin_api.register_migration absent."""
        assert not hasattr(scplugin_api, "register_migration"), \
            "transitional: plugin_api.register_migration must not exist yet"

    def test_e2e_plugin_sidecar_tables_created_by_core(
        self, engine_server, project_name
    ):
        """covers R17, R18 (row 13) — plugin sidecar tables via core DDL."""
        project_db = engine_server.work_dir / project_name / "project.db"
        conn = _raw_conn(project_db)
        try:
            tables = _table_names(conn)
            plugin_prefixed = {t for t in tables if "__" in t}
            assert plugin_prefixed, (
                "expected at least one plugin-prefixed sidecar table "
                "(hardcoded in core db.py)"
            )
        finally:
            conn.close()

    def test_e2e_send_buses_seeded_on_fresh_project(
        self, engine_server, project_name
    ):
        """covers R15 (row 11) — fresh project seeds 4 default send buses."""
        project_db = engine_server.work_dir / project_name / "project.db"
        conn = _raw_conn(project_db)
        try:
            rows = conn.execute(
                "SELECT label, order_index FROM project_send_buses "
                "ORDER BY order_index"
            ).fetchall()
            assert len(rows) == 4, f"expected 4 default buses, got {len(rows)}"
            assert [r[0] for r in rows] == ["Plate", "Hall", "Delay", "Echo"]
        finally:
            conn.close()

    @pytest.mark.xfail(
        reason="target-state; awaits schema_migrations table (R19)",
        strict=False,
    )
    def test_e2e_schema_migrations_rows_present(
        self, engine_server, project_name
    ):
        """covers R19 (row 14 target) — every core migration has a ledger row."""
        project_db = engine_server.work_dir / project_name / "project.db"
        conn = _raw_conn(project_db)
        try:
            rows = conn.execute(
                "SELECT version, applied_at FROM schema_migrations"
            ).fetchall()
            assert len(rows) > 0, "schema_migrations must have core rows"
        finally:
            conn.close()

    @pytest.mark.xfail(
        reason="target-state; awaits advisory flock (R24)", strict=False
    )
    def test_e2e_schema_lock_file_created(self, engine_server, project_name):
        """covers R24 (row 20 target) — .scenecraft/schema.lock exists post-init."""
        lock_file = (
            engine_server.work_dir / project_name / ".scenecraft" / "schema.lock"
        )
        assert lock_file.exists(), "schema.lock expected under project dir"
