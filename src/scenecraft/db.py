"""SQLite storage layer for scenecraft projects.

Replaces YAML read/write with instant SQL operations.
Each project gets its own `project.db` in its .scenecraft_work directory.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time as _time
import uuid
from pathlib import Path
from contextlib import contextmanager
from typing import Any


def generate_id(prefix: str) -> str:
    """Generate a UUID-based entity ID with a prefix.

    Format: {prefix}_{hex8} (e.g., kf_a3f7c21b, tr_9e4b0d12).
    """
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _retry_on_locked(fn, max_retries=5, delay=0.2):
    """Retry a DB operation on sqlite3.OperationalError (database is locked)."""
    for attempt in range(max_retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                _time.sleep(delay * (attempt + 1))
            else:
                raise


# Per-project connection pool (one connection per thread)
_connections: dict[str, sqlite3.Connection] = {}
_conn_lock = threading.Lock()


_migrated_dbs: set[str] = set()  # tracks which DBs have been migrated this process

def get_db(project_dir: Path, db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get or create a SQLite connection for a project directory.

    If `db_path` is provided, use that path directly (for session working copies).
    Otherwise, use the default `project_dir/project.db`.
    """
    if db_path is not None:
        db_path = str(db_path)
    else:
        db_path = str(project_dir / "project.db")
    thread_key = f"{db_path}:{threading.current_thread().ident}"

    with _conn_lock:
        if thread_key not in _connections:
            conn = sqlite3.connect(db_path, check_same_thread=False, timeout=60)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=60000")
            if db_path not in _migrated_dbs:
                _ensure_schema(conn)
                # Task-93: purge undo_log rows under undo_group=0. These come
                # from seed inserts in `_ensure_schema` (default audio_track,
                # send buses, etc.) that fire the per-table undo triggers
                # before any explicit `undo_begin`. R10's orphan sweep in
                # `undo_begin` only deletes rows whose undo_group is in the
                # pruned-groups set, so group-0 rows would otherwise grow
                # unbounded across boots. They have no recovery value: the
                # rows correspond to schema-seeded defaults that re-seed on
                # next bootstrap if missing.
                try:
                    conn.execute("DELETE FROM undo_log WHERE undo_group = 0")
                    conn.commit()
                except sqlite3.OperationalError:
                    # Very-fresh DB where undo_log doesn't exist yet — fine.
                    pass
                _migrated_dbs.add(db_path)
            _connections[thread_key] = conn
        return _connections[thread_key]


def close_db(project_dir: Path, db_path: Path | str | None = None):
    """Close all connections for a project (or a specific session DB path)."""
    if db_path is not None:
        db_path = str(db_path)
    else:
        db_path = str(project_dir / "project.db")
    with _conn_lock:
        to_remove = [k for k in _connections if k.startswith(db_path)]
        for k in to_remove:
            _connections[k].close()
            del _connections[k]


@contextmanager
def transaction(project_dir: Path):
    """Context manager for a database transaction."""
    conn = get_db(project_dir)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── M13 task-45: default send-bus fixtures ──────────────────────────
# Spec §Behavior "Configuring Send Buses": 2 reverb (Plate, Hall), 1 delay,
# 1 echo, in that order. Static-param payloads are JSON-serialized and mirror
# the `EffectTypeSpec` defaults the frontend mixer consults when it builds the
# per-bus WebAudio node (ConvolverNode for reverb, DelayNode for delay/echo).
# IR assets themselves ship in T47; here we only reference them by filename.
_DEFAULT_SEND_BUSES: tuple[dict, ...] = (
    {"bus_type": "reverb", "label": "Plate",
     "static_params": {"ir": "plate.wav"}},
    {"bus_type": "reverb", "label": "Hall",
     "static_params": {"ir": "hall.wav"}},
    {"bus_type": "delay", "label": "Delay",
     "static_params": {"time_division": "1/4", "feedback": 0.35}},
    {"bus_type": "echo", "label": "Echo",
     "static_params": {"time_ms": 120.0, "feedback": 0.0, "tone": 0.5}},
)


def _seed_default_send_buses(conn: sqlite3.Connection) -> list[str]:
    """INSERT the 4 default send buses into an empty project_send_buses table.

    Returns the list of generated bus IDs in order_index order. Called from
    `_ensure_schema` when the table is empty (fresh project OR migration of a
    pre-M13 project). Callers outside schema bootstrap should generally not
    invoke this directly.
    """
    ids: list[str] = []
    for i, bus in enumerate(_DEFAULT_SEND_BUSES):
        bus_id = generate_id("bus")
        conn.execute(
            "INSERT INTO project_send_buses (id, bus_type, label, order_index, static_params) "
            "VALUES (?, ?, ?, ?, ?)",
            (bus_id, bus["bus_type"], bus["label"], i, json.dumps(bus["static_params"])),
        )
        ids.append(bus_id)
    return ids


def _ensure_schema(conn: sqlite3.Connection):
    """Create tables if they don't exist."""
    # Greenfield (M9 task-82): if legacy `volume REAL` column exists on
    # audio_tracks or audio_clips, drop both tables so they get recreated
    # with the new `volume_curve TEXT` column. Acceptable because audio
    # clips have never been user-populated in production.
    for _tbl in ("audio_clips", "audio_tracks"):
        _cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_tbl})").fetchall()}
        if "volume" in _cols and "volume_curve" not in _cols:
            conn.execute(f"DROP TABLE IF EXISTS {_tbl}")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS keyframes (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            section TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            selected INTEGER,
            candidates TEXT NOT NULL DEFAULT '[]',
            context TEXT,
            deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS transitions (
            id TEXT PRIMARY KEY,
            from_kf TEXT NOT NULL,
            to_kf TEXT NOT NULL,
            duration_seconds REAL NOT NULL DEFAULT 0,
            slots INTEGER NOT NULL DEFAULT 1,
            action TEXT NOT NULL DEFAULT '',
            use_global_prompt INTEGER NOT NULL DEFAULT 0,
            selected TEXT NOT NULL DEFAULT '[]',
            remap TEXT NOT NULL DEFAULT '{"method":"linear","target_duration":0}',
            deleted_at TEXT,
            include_section_desc INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS effects (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL DEFAULT 'pulse',
            time REAL NOT NULL DEFAULT 0,
            intensity REAL NOT NULL DEFAULT 0.8,
            duration REAL NOT NULL DEFAULT 0.2
        );

        CREATE TABLE IF NOT EXISTS suppressions (
            id TEXT PRIMARY KEY,
            from_time REAL NOT NULL,
            to_time REAL NOT NULL,
            effect_types TEXT
        );

        CREATE TABLE IF NOT EXISTS bench (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            source_path TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prompt_roster (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            template TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'general'
        );

        CREATE TABLE IF NOT EXISTS markers (
            id TEXT PRIMARY KEY,
            time REAL NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT 'note'
        );

        CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT 'Track 1',
            z_order INTEGER NOT NULL DEFAULT 0,
            blend_mode TEXT NOT NULL DEFAULT 'normal',
            base_opacity REAL NOT NULL DEFAULT 1.0,
            muted INTEGER NOT NULL DEFAULT 0,
            solo INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS opacity_keyframes (
            id TEXT PRIMARY KEY,
            track_id TEXT NOT NULL,
            time REAL NOT NULL,
            opacity REAL NOT NULL DEFAULT 1.0
        );

        CREATE INDEX IF NOT EXISTS idx_keyframes_timestamp ON keyframes(timestamp);
        CREATE INDEX IF NOT EXISTS idx_keyframes_deleted ON keyframes(deleted_at);
        CREATE INDEX IF NOT EXISTS idx_transitions_from ON transitions(from_kf);
        CREATE INDEX IF NOT EXISTS idx_transitions_to ON transitions(to_kf);
        CREATE INDEX IF NOT EXISTS idx_transitions_deleted ON transitions(deleted_at);
        CREATE INDEX IF NOT EXISTS idx_opacity_kf_track ON opacity_keyframes(track_id, time);

        CREATE TABLE IF NOT EXISTS transition_effects (
            id TEXT PRIMARY KEY,
            transition_id TEXT NOT NULL,
            type TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            z_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_tr_effects ON transition_effects(transition_id);

        -- Candidate pool model (see design/local.candidate-pool-migration.md)
        -- pool_segments is the authoritative record of every video file in pool/segments/.
        -- Files are UUID-named on disk; original_filename / original_filepath preserve
        -- user-facing provenance for imports. Label is editable; created_by is not.
        CREATE TABLE IF NOT EXISTS pool_segments (
            id TEXT PRIMARY KEY,
            pool_path TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            original_filename TEXT,
            original_filepath TEXT,
            label TEXT NOT NULL DEFAULT '',
            generation_params TEXT,
            created_at TEXT NOT NULL,
            duration_seconds REAL,
            width INTEGER,
            height INTEGER,
            byte_size INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_pool_segments_kind ON pool_segments(kind);
        CREATE INDEX IF NOT EXISTS idx_pool_segments_created_by ON pool_segments(created_by);

        -- Normalized tag table (not a JSON column) — scales to 10k+ segments with
        -- indexed queries and merge-friendly row-level semantics.
        CREATE TABLE IF NOT EXISTS pool_segment_tags (
            pool_segment_id TEXT NOT NULL REFERENCES pool_segments(id),
            tag TEXT NOT NULL,
            tagged_by TEXT NOT NULL DEFAULT '',
            tagged_at TEXT NOT NULL,
            PRIMARY KEY (pool_segment_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_pool_segment_tags_tag ON pool_segment_tags(tag);
        CREATE INDEX IF NOT EXISTS idx_pool_segment_tags_segment ON pool_segment_tags(pool_segment_id);

        -- Junction mapping transitions to pool segments. Rank is derived from
        -- added_at (ORDER BY added_at ASC) — no stored rank column.
        CREATE TABLE IF NOT EXISTS tr_candidates (
            transition_id TEXT NOT NULL,
            slot INTEGER NOT NULL DEFAULT 0,
            pool_segment_id TEXT NOT NULL REFERENCES pool_segments(id),
            added_at TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (transition_id, slot, pool_segment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tr_candidates_tr ON tr_candidates(transition_id);
        CREATE INDEX IF NOT EXISTS idx_tr_candidates_segment ON tr_candidates(pool_segment_id);
        CREATE INDEX IF NOT EXISTS idx_tr_candidates_order ON tr_candidates(transition_id, slot, added_at);

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'local',
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            images TEXT,
            tool_calls TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages(user_id, created_at);

        CREATE TABLE IF NOT EXISTS audio_tracks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT 'Audio Track 1',
            display_order INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0,
            muted INTEGER NOT NULL DEFAULT 0,
            solo INTEGER NOT NULL DEFAULT 0,
            volume_curve TEXT NOT NULL DEFAULT '[[0,0],[1,0]]'
        );

        CREATE TABLE IF NOT EXISTS audio_clips (
            id TEXT PRIMARY KEY,
            track_id TEXT NOT NULL,
            source_path TEXT NOT NULL DEFAULT '',
            start_time REAL NOT NULL DEFAULT 0,
            end_time REAL NOT NULL DEFAULT 0,
            source_offset REAL NOT NULL DEFAULT 0,
            volume_curve TEXT NOT NULL DEFAULT '[[0,0],[1,0]]',
            muted INTEGER NOT NULL DEFAULT 0,
            remap TEXT NOT NULL DEFAULT '{"method":"linear","target_duration":0}',
            label TEXT,
            deleted_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_audio_clips_track ON audio_clips(track_id);
        CREATE INDEX IF NOT EXISTS idx_audio_clips_deleted ON audio_clips(deleted_at);

        CREATE TABLE IF NOT EXISTS audio_clip_links (
            audio_clip_id TEXT NOT NULL,
            transition_id TEXT NOT NULL,
            offset REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (audio_clip_id, transition_id)
        );
        CREATE INDEX IF NOT EXISTS idx_acl_transition ON audio_clip_links(transition_id);
        CREATE INDEX IF NOT EXISTS idx_acl_audio_clip ON audio_clip_links(audio_clip_id);

        -- Junction mapping audio_clips to pool_segments. Mirrors tr_candidates
        -- but for audio: each clip can have N alternate sources (isolated stems,
        -- regenerated TTS, plugin-processed variants), with one optionally
        -- promoted to selected via audio_clips.selected.
        CREATE TABLE IF NOT EXISTS audio_candidates (
            audio_clip_id     TEXT NOT NULL REFERENCES audio_clips(id),
            pool_segment_id   TEXT NOT NULL REFERENCES pool_segments(id),
            added_at          TEXT NOT NULL,
            source            TEXT NOT NULL,
            PRIMARY KEY (audio_clip_id, pool_segment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_audio_cand_clip ON audio_candidates(audio_clip_id);
        CREATE INDEX IF NOT EXISTS idx_audio_cand_seg ON audio_candidates(pool_segment_id);

        -- M11 task-100b: multi-stem isolation runs. One audio_isolations row per
        -- invocation of an isolation plugin (vocal/background split, etc.);
        -- isolation_stems is the junction from run → pool_segments rows (one
        -- per emitted stem, typed 'vocal' | 'background' for MVP).
        CREATE TABLE IF NOT EXISTS audio_isolations (
            id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            model TEXT NOT NULL,
            range_mode TEXT NOT NULL,
            trim_in REAL,
            trim_out REAL,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_isolations_entity
            ON audio_isolations(entity_type, entity_id);
        CREATE INDEX IF NOT EXISTS idx_isolations_created
            ON audio_isolations(created_at);

        -- FK is DEFERRABLE INITIALLY DEFERRED so undo/redo can replay rows in
        -- seq order (stems before their parent isolation row) inside a single
        -- commit — SQLite defers FK checks to commit boundary.
        CREATE TABLE IF NOT EXISTS isolation_stems (
            isolation_id TEXT NOT NULL,
            pool_segment_id TEXT NOT NULL REFERENCES pool_segments(id),
            stem_type TEXT NOT NULL,
            PRIMARY KEY (isolation_id, pool_segment_id),
            FOREIGN KEY (isolation_id) REFERENCES audio_isolations(id)
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE INDEX IF NOT EXISTS idx_isolation_stems_run
            ON isolation_stems(isolation_id);
        CREATE INDEX IF NOT EXISTS idx_isolation_stems_segment
            ON isolation_stems(pool_segment_id);

        -- M16 music generation: plugin-owned tables under the __ prefix
        -- convention (see spec local.music-generation-plugin.md, R7/R8/R11).
        -- Auth FKs on created_by etc. are deferred per 2026-04-23 dev directive;
        -- values default to '' until auth milestone ships.
        CREATE TABLE IF NOT EXISTS generate_music__generations (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL CHECK (action IN ('auto', 'custom')),
            model TEXT NOT NULL,
            style TEXT,
            lyrics TEXT,
            title TEXT,
            instrumental INTEGER NOT NULL CHECK (instrumental IN (0, 1)),
            gender TEXT,
            singer_id TEXT,
            task_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
            error TEXT,
            entity_type TEXT CHECK (entity_type IN ('audio_clip', 'transition') OR entity_type IS NULL),
            entity_id TEXT,
            reused_from TEXT REFERENCES generate_music__generations(id),
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gm_gen_entity
            ON generate_music__generations(entity_type, entity_id);
        CREATE INDEX IF NOT EXISTS idx_gm_gen_status
            ON generate_music__generations(status);
        CREATE INDEX IF NOT EXISTS idx_gm_gen_created
            ON generate_music__generations(created_at);

        CREATE TABLE IF NOT EXISTS generate_music__tracks (
            generation_id TEXT NOT NULL REFERENCES generate_music__generations(id),
            pool_segment_id TEXT NOT NULL REFERENCES pool_segments(id),
            musicful_task_id TEXT NOT NULL,
            song_title TEXT,
            duration_seconds REAL,
            cover_url TEXT,
            created_by TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (generation_id, pool_segment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_gm_tracks_pool
            ON generate_music__tracks(pool_segment_id);

        -- M18 foley generation: plugin-owned tables under the __ prefix
        -- convention. Mirrors generate_music shape exactly. MVP enforces
        -- variant_count=1 at the API boundary; schema supports N for
        -- forward-looking multi-variant.
        -- See agent/design/local.foley-generation-plugin.md "Schema" and
        -- agent/clarifications/clarification-12-foley-generation-plugin.md.
        CREATE TABLE IF NOT EXISTS generate_foley__generations (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',

            -- mode + input
            mode TEXT NOT NULL CHECK (mode IN ('t2fx', 'v2fx')),
            prompt TEXT,
            duration_seconds REAL,
            source_candidate_id TEXT,
            source_in_seconds REAL,
            source_out_seconds REAL,

            -- model params
            model TEXT NOT NULL,
            negative_prompt TEXT,
            cfg_strength REAL,
            seed INTEGER,

            -- kickoff context (auto-stamped onto pool_segments)
            entity_type TEXT CHECK (entity_type IN ('transition') OR entity_type IS NULL),
            entity_id TEXT,

            -- forward-looking multi-variant
            variant_count INTEGER NOT NULL DEFAULT 1,

            -- execution
            status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
            error TEXT,

            started_at TEXT,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_gf_gen_status
            ON generate_foley__generations(status);
        CREATE INDEX IF NOT EXISTS idx_gf_gen_entity
            ON generate_foley__generations(entity_type, entity_id);
        CREATE INDEX IF NOT EXISTS idx_gf_gen_created
            ON generate_foley__generations(created_at);

        CREATE TABLE IF NOT EXISTS generate_foley__tracks (
            generation_id TEXT NOT NULL REFERENCES generate_foley__generations(id),
            pool_segment_id TEXT NOT NULL REFERENCES pool_segments(id),
            variant_index INTEGER NOT NULL,
            replicate_prediction_id TEXT NOT NULL,
            duration_seconds REAL,
            spend_ledger_id TEXT,
            created_by TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (generation_id, pool_segment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_gf_tracks_pool
            ON generate_foley__tracks(pool_segment_id);

        -- Transcribe plugin: one `transcribe__runs` row per completed
        -- transcription invocation, keyed by (clip_id, model, word_timestamps)
        -- so re-runs with identical inputs hit the cache. Segments live in
        -- the child table keyed by run_id with preserved ordering. `output`
        -- stores the raw provider response for debugging / re-parsing.
        CREATE TABLE IF NOT EXISTS transcribe__runs (
            id TEXT PRIMARY KEY,
            clip_id TEXT NOT NULL,
            model TEXT NOT NULL,
            model_slug TEXT NOT NULL,
            language TEXT,
            word_timestamps INTEGER NOT NULL DEFAULT 0 CHECK (word_timestamps IN (0, 1)),
            text TEXT NOT NULL DEFAULT '',
            detected_language TEXT,
            duration_seconds REAL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
            error TEXT,
            raw_output TEXT,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_transcribe_runs_clip
            ON transcribe__runs(clip_id);
        CREATE INDEX IF NOT EXISTS idx_transcribe_runs_cache
            ON transcribe__runs(clip_id, model, word_timestamps);
        CREATE INDEX IF NOT EXISTS idx_transcribe_runs_created
            ON transcribe__runs(created_at);

        CREATE TABLE IF NOT EXISTS transcribe__segments (
            run_id TEXT NOT NULL REFERENCES transcribe__runs(id),
            seq INTEGER NOT NULL,
            start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL,
            text TEXT NOT NULL,
            words_json TEXT,
            PRIMARY KEY (run_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_transcribe_segments_run
            ON transcribe__segments(run_id);

        -- light_show plugin (M17 MVP): one row per rigged fixture in the
        -- project's DMX simulation. Positions and rotations drive the 3D
        -- preview panel's three.js scene. ``role`` is a denormalized
        -- discriminant used by scene DSLs for role-based targeting
        -- (@role.moving_head, @role.par, ...).
        CREATE TABLE IF NOT EXISTS light_show__fixtures (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            label TEXT NOT NULL,
            position_x REAL NOT NULL DEFAULT 0,
            position_y REAL NOT NULL DEFAULT 0,
            position_z REAL NOT NULL DEFAULT 0,
            rotation_x REAL NOT NULL DEFAULT 0,
            rotation_y REAL NOT NULL DEFAULT 0,
            rotation_z REAL NOT NULL DEFAULT 0,
            -- DMX patch (nullable; auto-patcher fills gaps when null).
            -- ``dmx_address`` is 1-based start address of the fixture's
            -- channel block in its universe. ``dmx_channel_count`` overrides
            -- the role-default channel count (6 for moving_head, 4 for par)
            -- when a fixture is in a non-default mode (e.g. 16-channel mover).
            dmx_universe INTEGER,
            dmx_address INTEGER,
            dmx_channel_count INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_light_show_fixtures_role
            ON light_show__fixtures(role);
        CREATE INDEX IF NOT EXISTS idx_light_show_fixtures_dmx
            ON light_show__fixtures(dmx_universe, dmx_address);

        -- Per-fixture manual channel overrides. NULL column = that channel is
        -- NOT overridden (scene-driven); non-NULL = the value the shader uses
        -- regardless of what the active scene writes. ``color`` is a single
        -- override (all 3 RGB channels together) — set all three or none.
        -- Enables chat-driven 'force MH 2 to bright red' without having to
        -- change the active scene.
        CREATE TABLE IF NOT EXISTS light_show__overrides (
            fixture_id TEXT PRIMARY KEY REFERENCES light_show__fixtures(id) ON DELETE CASCADE,
            intensity REAL,
            color_r REAL,
            color_g REAL,
            color_b REAL,
            pan REAL,
            tilt REAL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- light_show screens: flat video panels rendered in the 3D preview
        -- as textured planes. MVP renders the scenecraft main-timeline
        -- frame preview onto every screen (shared texture); per-screen
        -- timelines are a post-MVP follow-up. Dimensions in meters.
        CREATE TABLE IF NOT EXISTS light_show__screens (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            position_x REAL NOT NULL DEFAULT 0,
            position_y REAL NOT NULL DEFAULT 0,
            position_z REAL NOT NULL DEFAULT 0,
            rotation_x REAL NOT NULL DEFAULT 0,
            rotation_y REAL NOT NULL DEFAULT 0,
            rotation_z REAL NOT NULL DEFAULT 0,
            width REAL NOT NULL DEFAULT 4,
            height REAL NOT NULL DEFAULT 2.25,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- ── M19 Light Show Scene Editor — three-tier data model ───────────
        --
        -- light_show__scenes: the scene library. Each row is a parameterized
        -- instance of a primitive (rotating_head, static_color, ...). params
        -- is stored SPARSE — only keys explicitly overridden by the user
        -- live in params_json; catalog defaults are merged at evaluator time
        -- (NOT at insert/update). This ensures list → modify → set round-
        -- trips don't accidentally promote defaults to explicit overrides.
        -- Spec R1.
        CREATE TABLE IF NOT EXISTS light_show__scenes (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            type TEXT NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- light_show__scene_placements: pre-programmed timeline schedule.
        -- Each row places a library scene on the main playhead from
        -- start_time to end_time with optional fade in/out and a display
        -- order for overlap resolution. Spec R2.
        CREATE TABLE IF NOT EXISTS light_show__scene_placements (
            id TEXT PRIMARY KEY,
            scene_id TEXT NOT NULL REFERENCES light_show__scenes(id),
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            display_order INTEGER NOT NULL DEFAULT 0,
            fade_in_sec REAL NOT NULL DEFAULT 0,
            fade_out_sec REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_light_show_placements_time
            ON light_show__scene_placements(start_time, end_time);

        -- light_show__live_override: single-slot live trigger that wins
        -- over any timeline placement. Always 0 or 1 row (id checked to
        -- 'current' so any other id is a write error). Either references
        -- a library scene by id, OR carries an inline directive (type +
        -- inline_params_json) — the CHECK constraint enforces exactly
        -- one. Spec R3.
        CREATE TABLE IF NOT EXISTS light_show__live_override (
            id TEXT PRIMARY KEY CHECK (id = 'current'),
            scene_id TEXT REFERENCES light_show__scenes(id),
            inline_type TEXT,
            inline_params_json TEXT,
            label TEXT NOT NULL,
            fade_in_sec REAL NOT NULL DEFAULT 0,
            fade_out_sec REAL NOT NULL DEFAULT 0,
            activated_at TEXT NOT NULL DEFAULT (datetime('now')),
            deactivation_started_at TEXT,
            CHECK (
                (scene_id IS NOT NULL AND inline_type IS NULL AND inline_params_json IS NULL)
                OR
                (scene_id IS NULL AND inline_type IS NOT NULL AND inline_params_json IS NOT NULL)
            )
        );

        CREATE TABLE IF NOT EXISTS sections (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            start TEXT NOT NULL DEFAULT '0:00',
            "end" TEXT,
            mood TEXT NOT NULL DEFAULT '',
            energy TEXT NOT NULL DEFAULT '',
            instruments TEXT NOT NULL DEFAULT '[]',
            motifs TEXT NOT NULL DEFAULT '[]',
            events TEXT NOT NULL DEFAULT '[]',
            visual_direction TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS checkpoints (
            filename TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        -- M13 task-45: effect-curves + macro-panel schema (spec R1-R5).
        -- CORE tables (not plugin-owned; no __ prefix). Per-project scope is
        -- enforced by the project.db location, not an explicit project_id FK.
        --
        -- Master-bus effects (post-M13): ``track_id`` is nullable. NULL means
        -- the effect processes the SUMMED master bus (output of ``masterGain``
        -- before ``destination``). Non-NULL rows scope to an audio track as
        -- before. A migration below rewrites legacy DBs created with
        -- ``track_id NOT NULL`` in place (``track_id_is_nullable`` check via
        -- ``PRAGMA table_info``).
        CREATE TABLE IF NOT EXISTS track_effects (
            id TEXT PRIMARY KEY,
            track_id TEXT REFERENCES audio_tracks(id) ON DELETE CASCADE,
            effect_type TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            static_params TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS track_effects_track_order
            ON track_effects(track_id, order_index);

        CREATE TABLE IF NOT EXISTS effect_curves (
            id TEXT PRIMARY KEY,
            effect_id TEXT NOT NULL REFERENCES track_effects(id) ON DELETE CASCADE,
            param_name TEXT NOT NULL,
            points TEXT NOT NULL,
            interpolation TEXT NOT NULL DEFAULT 'bezier',
            visible INTEGER NOT NULL DEFAULT 0,
            UNIQUE(effect_id, param_name)
        );
        CREATE INDEX IF NOT EXISTS idx_effect_curves_effect
            ON effect_curves(effect_id);

        CREATE TABLE IF NOT EXISTS project_send_buses (
            id TEXT PRIMARY KEY,
            bus_type TEXT NOT NULL,
            label TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            static_params TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_send_buses_order
            ON project_send_buses(order_index);

        CREATE TABLE IF NOT EXISTS track_sends (
            track_id TEXT NOT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE,
            bus_id TEXT NOT NULL REFERENCES project_send_buses(id) ON DELETE CASCADE,
            level REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (track_id, bus_id)
        );
        CREATE INDEX IF NOT EXISTS idx_track_sends_bus
            ON track_sends(bus_id);

        CREATE TABLE IF NOT EXISTS project_frequency_labels (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            freq_min_hz REAL NOT NULL,
            freq_max_hz REAL NOT NULL
        );

        -- Phase 3: cached librosa analysis (quantitative / ground-truth).
        -- Cache key: (source_segment_id, analyzer_version, params_hash).
        -- Source segments are immutable in this codebase so the cache is
        -- stable across timeline edits; clip trim changes don't invalidate.
        CREATE TABLE IF NOT EXISTS dsp_analysis_runs (
            id TEXT PRIMARY KEY,
            source_segment_id TEXT NOT NULL REFERENCES pool_segments(id) ON DELETE CASCADE,
            analyzer_version TEXT NOT NULL,
            params_hash TEXT NOT NULL,
            analyses_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_segment_id, analyzer_version, params_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_dsp_runs_source
            ON dsp_analysis_runs(source_segment_id);

        -- Time-series datapoints that fit (time, value). Covers onsets, RMS
        -- envelope, spectral centroid/rolloff/flux, ZCR, pitch estimate.
        -- extra_json for the rare case a single REAL isn't enough.
        CREATE TABLE IF NOT EXISTS dsp_datapoints (
            run_id TEXT NOT NULL REFERENCES dsp_analysis_runs(id) ON DELETE CASCADE,
            data_type TEXT NOT NULL,
            time_s REAL NOT NULL,
            value REAL NOT NULL,
            extra_json TEXT,
            PRIMARY KEY (run_id, data_type, time_s)
        );
        CREATE INDEX IF NOT EXISTS idx_dsp_datapoints_type_time
            ON dsp_datapoints(run_id, data_type, time_s);

        -- Time-ranged regions. E.g. vocal_presence ranges, drops, silence.
        CREATE TABLE IF NOT EXISTS dsp_sections (
            run_id TEXT NOT NULL REFERENCES dsp_analysis_runs(id) ON DELETE CASCADE,
            start_s REAL NOT NULL,
            end_s REAL NOT NULL,
            section_type TEXT NOT NULL,
            label TEXT,
            confidence REAL,
            PRIMARY KEY (run_id, start_s, section_type)
        );
        CREATE INDEX IF NOT EXISTS idx_dsp_sections_type_start
            ON dsp_sections(run_id, section_type, start_s);

        -- Global scalars for the run: tempo_bpm, peak_db, global_rms, etc.
        CREATE TABLE IF NOT EXISTS dsp_scalars (
            run_id TEXT NOT NULL REFERENCES dsp_analysis_runs(id) ON DELETE CASCADE,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (run_id, metric)
        );

        -- Phase 3: cached LLM semantic descriptions (qualitative / vibes).
        -- Cache key: (source_segment_id, model, prompt_version). Prompt
        -- iteration produces new runs; old runs persist for A/B comparison.
        CREATE TABLE IF NOT EXISTS audio_description_runs (
            id TEXT PRIMARY KEY,
            source_segment_id TEXT NOT NULL REFERENCES pool_segments(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            chunk_size_s REAL NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_segment_id, model, prompt_version)
        );
        CREATE INDEX IF NOT EXISTS idx_audio_desc_runs_source
            ON audio_description_runs(source_segment_id);

        -- Time-ranged structured properties: mood, section_type, energy,
        -- vocal_style, genre, instrumentation, etc.
        CREATE TABLE IF NOT EXISTS audio_descriptions (
            run_id TEXT NOT NULL REFERENCES audio_description_runs(id) ON DELETE CASCADE,
            start_s REAL NOT NULL,
            end_s REAL NOT NULL,
            property TEXT NOT NULL,
            value_text TEXT,
            value_num REAL,
            confidence REAL,
            raw_json TEXT,
            PRIMARY KEY (run_id, start_s, property)
        );
        CREATE INDEX IF NOT EXISTS idx_audio_descriptions_property_time
            ON audio_descriptions(run_id, property, start_s);

        -- Segment-global qualitative properties: key, global_genre,
        -- vocal_gender, global_mood, etc.
        CREATE TABLE IF NOT EXISTS audio_description_scalars (
            run_id TEXT NOT NULL REFERENCES audio_description_runs(id) ON DELETE CASCADE,
            property TEXT NOT NULL,
            value_text TEXT,
            value_num REAL,
            confidence REAL,
            PRIMARY KEY (run_id, property)
        );

        -- M15: cached master-bus mix analysis.
        -- Cache key: (mix_graph_hash, start_time_s, end_time_s, sample_rate,
        --            analyzer_version).
        -- The mix_graph_hash captures every mix-affecting factor (tracks,
        -- clips, effects, curves, sends). See scenecraft/mix_graph_hash.py.
        CREATE TABLE IF NOT EXISTS mix_analysis_runs (
            id TEXT PRIMARY KEY,
            mix_graph_hash TEXT NOT NULL,
            start_time_s REAL NOT NULL,
            end_time_s REAL NOT NULL,
            sample_rate INTEGER NOT NULL,
            analyzer_version TEXT NOT NULL,
            analyses_json TEXT NOT NULL,
            rendered_path TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(mix_graph_hash, start_time_s, end_time_s, sample_rate, analyzer_version)
        );
        CREATE INDEX IF NOT EXISTS idx_mix_runs_hash ON mix_analysis_runs(mix_graph_hash);

        -- Time-series datapoints from the analyzer: rms, short_term_lufs,
        -- spectral_centroid, etc. Same shape as dsp_datapoints.
        CREATE TABLE IF NOT EXISTS mix_datapoints (
            run_id TEXT NOT NULL REFERENCES mix_analysis_runs(id) ON DELETE CASCADE,
            data_type TEXT NOT NULL,
            time_s REAL NOT NULL,
            value REAL NOT NULL,
            extra_json TEXT,
            PRIMARY KEY (run_id, data_type, time_s)
        );
        CREATE INDEX IF NOT EXISTS idx_mix_datapoints_type_time
            ON mix_datapoints(run_id, data_type, time_s);

        -- Time-ranged regions: clipping_event, silence, etc.
        CREATE TABLE IF NOT EXISTS mix_sections (
            run_id TEXT NOT NULL REFERENCES mix_analysis_runs(id) ON DELETE CASCADE,
            start_s REAL NOT NULL,
            end_s REAL NOT NULL,
            section_type TEXT NOT NULL,
            label TEXT,
            confidence REAL,
            PRIMARY KEY (run_id, start_s, section_type)
        );

        -- Global scalars: peak_db, true_peak_db, lufs_integrated,
        -- dynamic_range, clip_count.
        CREATE TABLE IF NOT EXISTS mix_scalars (
            run_id TEXT NOT NULL REFERENCES mix_analysis_runs(id) ON DELETE CASCADE,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (run_id, metric)
        );

        -- M16: bounce_audio cache. Each row is one rendered WAV at
        -- pool/bounces/<composite_hash>.wav. composite_hash = SHA-256 over
        -- (mix_graph_hash + selection + format) — see scenecraft/bounce_hash.py.
        -- UNIQUE on composite_hash guarantees cache-hit semantics:
        -- identical mix + selection + format reuses the same row and file.
        CREATE TABLE IF NOT EXISTS audio_bounces (
            id TEXT PRIMARY KEY,
            composite_hash TEXT NOT NULL UNIQUE,
            start_time_s REAL NOT NULL,
            end_time_s REAL NOT NULL,
            mode TEXT NOT NULL,
            selection_json TEXT NOT NULL,
            sample_rate INTEGER NOT NULL,
            bit_depth INTEGER NOT NULL,
            channels INTEGER NOT NULL DEFAULT 2,
            rendered_path TEXT,
            size_bytes INTEGER,
            duration_s REAL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audio_bounces_hash ON audio_bounces(composite_hash);
    """)

    # ── Undo system ──
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS undo_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            undo_group INTEGER NOT NULL,
            sql_text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS undo_groups (
            id INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            undone INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS undo_state (
            key TEXT PRIMARY KEY,
            value INTEGER
        );
        CREATE TABLE IF NOT EXISTS redo_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            undo_group INTEGER NOT NULL,
            sql_text TEXT NOT NULL
        );
        INSERT OR IGNORE INTO undo_state VALUES ('current_group', 0);
        INSERT OR IGNORE INTO undo_state VALUES ('active', 1);
    """)

    # ── Migration: add track_id to keyframes/transitions if missing ──
    cols = {row[1] for row in conn.execute("PRAGMA table_info(keyframes)").fetchall()}
    if "track_id" not in cols:
        conn.execute("ALTER TABLE keyframes ADD COLUMN track_id TEXT NOT NULL DEFAULT 'track_1'")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(transitions)").fetchall()}
    if "track_id" not in cols:
        conn.execute("ALTER TABLE transitions ADD COLUMN track_id TEXT NOT NULL DEFAULT 'track_1'")

    # Add label/label_color columns to keyframes if missing
    if "label" not in cols:
        conn.execute("ALTER TABLE keyframes ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    if "label_color" not in cols:
        conn.execute("ALTER TABLE keyframes ADD COLUMN label_color TEXT NOT NULL DEFAULT ''")

    # Add label/label_color/tags columns to transitions if missing
    tr_cols = {row[1] for row in conn.execute("PRAGMA table_info(transitions)").fetchall()}
    if "label" not in tr_cols:
        conn.execute("ALTER TABLE transitions ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    if "label_color" not in tr_cols:
        conn.execute("ALTER TABLE transitions ADD COLUMN label_color TEXT NOT NULL DEFAULT ''")
    if "tags" not in tr_cols:
        conn.execute("ALTER TABLE transitions ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")

    # Add blend_mode/opacity columns to keyframes if missing
    kf_cols = {row[1] for row in conn.execute("PRAGMA table_info(keyframes)").fetchall()}
    if "blend_mode" not in kf_cols:
        conn.execute("ALTER TABLE keyframes ADD COLUMN blend_mode TEXT NOT NULL DEFAULT ''")
    if "opacity" not in kf_cols:
        conn.execute("ALTER TABLE keyframes ADD COLUMN opacity REAL")
    if "refinement_prompt" not in kf_cols:
        conn.execute("ALTER TABLE keyframes ADD COLUMN refinement_prompt TEXT NOT NULL DEFAULT ''")

    # Add blend_mode/opacity columns to transitions if missing
    tr_cols2 = {row[1] for row in conn.execute("PRAGMA table_info(transitions)").fetchall()}
    if "blend_mode" not in tr_cols2:
        conn.execute("ALTER TABLE transitions ADD COLUMN blend_mode TEXT NOT NULL DEFAULT ''")
    if "opacity" not in tr_cols2:
        conn.execute("ALTER TABLE transitions ADD COLUMN opacity REAL")
    if "opacity_curve" not in tr_cols2:
        conn.execute("ALTER TABLE transitions ADD COLUMN opacity_curve TEXT")
    for curve_col in ("red_curve", "green_curve", "blue_curve", "black_curve", "hue_shift_curve", "saturation_curve", "invert_curve", "brightness_curve", "contrast_curve", "exposure_curve"):
        if curve_col not in tr_cols2:
            conn.execute(f"ALTER TABLE transitions ADD COLUMN {curve_col} TEXT")
    if "is_adjustment" not in tr_cols2:
        conn.execute("ALTER TABLE transitions ADD COLUMN is_adjustment INTEGER NOT NULL DEFAULT 0")
    if "chroma_key" not in tr_cols2:
        conn.execute("ALTER TABLE transitions ADD COLUMN chroma_key TEXT")
    for mask_col in ("mask_center_x", "mask_center_y", "mask_radius", "mask_feather", "transform_x", "transform_y"):
        if mask_col not in tr_cols2:
            conn.execute(f"ALTER TABLE transitions ADD COLUMN {mask_col} REAL")
    if "hidden" not in tr_cols2:
        conn.execute("ALTER TABLE transitions ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")

    # Add transform curve columns and migrate static values
    tr_cols3 = {row[1] for row in conn.execute("PRAGMA table_info(transitions)").fetchall()}
    for curve_col in ("transform_x_curve", "transform_y_curve", "transform_z_curve"):
        if curve_col not in tr_cols3:
            conn.execute(f"ALTER TABLE transitions ADD COLUMN {curve_col} TEXT")
    # Migrate existing static transform_x/transform_y to flat curves
    if "transform_x_curve" not in tr_cols3:
        rows = conn.execute("SELECT id, transform_x, transform_y FROM transitions WHERE transform_x IS NOT NULL OR transform_y IS NOT NULL").fetchall()
        for row in rows:
            tx = row[1] or 0
            ty = row[2] or 0
            if tx != 0:
                conn.execute("UPDATE transitions SET transform_x_curve = ? WHERE id = ?", (json.dumps([[0, tx], [1, tx]]), row[0]))
            if ty != 0:
                conn.execute("UPDATE transitions SET transform_y_curve = ? WHERE id = ?", (json.dumps([[0, ty], [1, ty]]), row[0]))

    # Split transform_z_curve (uniform scale) into transform_scale_x_curve +
    # transform_scale_y_curve (independent non-uniform scale). Copy the z
    # curve into both new columns so existing projects render identically
    # — the only change is that users CAN now animate x and y independently
    # going forward. See agent/design/local.split-z-into-scalex-scaley.md.
    # Idempotent: driven by column presence, safe to re-run.
    tr_cols4 = {row[1] for row in conn.execute("PRAGMA table_info(transitions)").fetchall()}
    if "transform_scale_x_curve" not in tr_cols4:
        conn.execute("ALTER TABLE transitions ADD COLUMN transform_scale_x_curve TEXT")
    if "transform_scale_y_curve" not in tr_cols4:
        conn.execute("ALTER TABLE transitions ADD COLUMN transform_scale_y_curve TEXT")
    # Copy existing z curves into both new columns. Only populate where the
    # new columns are still null, so re-running the migration doesn't
    # clobber user edits made after the initial migration.
    if "transform_z_curve" in tr_cols4:
        conn.execute(
            "UPDATE transitions "
            "SET transform_scale_x_curve = transform_z_curve, "
            "    transform_scale_y_curve = transform_z_curve "
            "WHERE transform_z_curve IS NOT NULL "
            "  AND transform_scale_x_curve IS NULL "
            "  AND transform_scale_y_curve IS NULL"
        )
        # Drop the old uniform-scale column. SQLite DROP COLUMN is available
        # in 3.35+ (2021). Wrap in try/except for two known failure modes:
        #   1. Older SQLite without DROP COLUMN support.
        #   2. Triggers on this table (e.g. transitions_update_undo) that
        #      reference OLD.transform_z_curve — SQLite rejects the drop
        #      rather than silently invalidating the trigger.
        # In both cases the column stays (inert — no code path reads it
        # after this migration) and the migration completes successfully.
        # Rebuilding history triggers to drop the column cleanly is
        # deferred until a broader schema-cleanup pass.
        try:
            conn.execute("ALTER TABLE transitions DROP COLUMN transform_z_curve")
        except sqlite3.OperationalError:
            pass

    # Add layer_effect_types to suppressions if missing
    sup_cols = {row[1] for row in conn.execute("PRAGMA table_info(suppressions)").fetchall()}
    if "layer_effect_types" not in sup_cols:
        conn.execute("ALTER TABLE suppressions ADD COLUMN layer_effect_types TEXT")

    # Add chroma_key column to tracks if missing
    track_cols = {row[1] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
    if "chroma_key" not in track_cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN chroma_key TEXT")
    if "hidden" not in track_cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    if "solo" not in track_cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN solo INTEGER NOT NULL DEFAULT 0")

    # Replace `enabled` with `muted` on tracks. Semantically `enabled=false`
    # always meant "mute this track" in the UI (tooltip literally said so).
    # Adding `muted` (back-filled from !enabled) and dropping `enabled`.
    if "muted" not in track_cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
        if "enabled" in track_cols:
            conn.execute("UPDATE tracks SET muted = CASE WHEN enabled = 0 THEN 1 ELSE 0 END")
    if "enabled" in track_cols:
        try:
            conn.execute("ALTER TABLE tracks DROP COLUMN enabled")
        except sqlite3.OperationalError:
            pass  # SQLite <3.35 — leave the column, it's unused going forward

    # Add solo column to audio_tracks if missing. When any track is solo'd,
    # non-solo tracks are effectively muted — same convention as Premiere /
    # Resolve. Multiple tracks can solo simultaneously.
    audio_track_cols = {row[1] for row in conn.execute("PRAGMA table_info(audio_tracks)").fetchall()}
    if "solo" not in audio_track_cols:
        conn.execute("ALTER TABLE audio_tracks ADD COLUMN solo INTEGER NOT NULL DEFAULT 0")

    # Drop `enabled` on audio_tracks. Was always OR'd with `muted` in the
    # mixer so redundant; consolidate to `muted`.
    if "enabled" in audio_track_cols:
        conn.execute("UPDATE audio_tracks SET muted = 1 WHERE enabled = 0")
        try:
            conn.execute("ALTER TABLE audio_tracks DROP COLUMN enabled")
        except sqlite3.OperationalError:
            pass

    # Add anchor_x/anchor_y columns to transitions if missing
    tr_cols4 = {row[1] for row in conn.execute("PRAGMA table_info(transitions)").fetchall()}
    for col in ("anchor_x", "anchor_y"):
        if col not in tr_cols4:
            conn.execute(f"ALTER TABLE transitions ADD COLUMN {col} REAL")

    # Add type column to markers if missing
    marker_cols = {row[1] for row in conn.execute("PRAGMA table_info(markers)").fetchall()}
    if "type" not in marker_cols:
        conn.execute("ALTER TABLE markers ADD COLUMN type TEXT NOT NULL DEFAULT 'note'")

    # Ensure default track exists
    try:
        if not conn.execute("SELECT 1 FROM tracks WHERE id = 'track_1'").fetchone():
            conn.execute("INSERT OR IGNORE INTO tracks (id, name, z_order, blend_mode, base_opacity, muted) VALUES ('track_1', 'Track 1', 0, 'normal', 1.0, 0)")
    except Exception:
        pass  # another thread may have inserted it

    # Add ingredients, negative_prompt, seed columns to transitions
    tr_cols5 = {row[1] for row in conn.execute("PRAGMA table_info(transitions)").fetchall()}
    if "ingredients" not in tr_cols5:
        conn.execute("ALTER TABLE transitions ADD COLUMN ingredients TEXT NOT NULL DEFAULT '[]'")
    if "negative_prompt" not in tr_cols5:
        conn.execute("ALTER TABLE transitions ADD COLUMN negative_prompt TEXT NOT NULL DEFAULT ''")
    if "seed" not in tr_cols5:
        conn.execute("ALTER TABLE transitions ADD COLUMN seed INTEGER")

    # Add last_modified_by column for attribution
    for table in ("keyframes", "transitions", "effects", "tracks"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "last_modified_by" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN last_modified_by TEXT NOT NULL DEFAULT ''")

    # M7: Add trim_in, trim_out, source_video_duration to transitions
    tr_cols6 = {row[1] for row in conn.execute("PRAGMA table_info(transitions)").fetchall()}
    if "trim_in" not in tr_cols6:
        conn.execute("ALTER TABLE transitions ADD COLUMN trim_in REAL NOT NULL DEFAULT 0")
    if "trim_out" not in tr_cols6:
        conn.execute("ALTER TABLE transitions ADD COLUMN trim_out REAL")
    if "source_video_duration" not in tr_cols6:
        conn.execute("ALTER TABLE transitions ADD COLUMN source_video_duration REAL")

    # M11: Add audio_clips.selected (FK to pool_segments.id) for the audio
    # candidate junction. Nullable; NULL means "use source_path as-is".
    ac_cols = {row[1] for row in conn.execute("PRAGMA table_info(audio_clips)").fetchall()}
    if "selected" not in ac_cols:
        conn.execute("ALTER TABLE audio_clips ADD COLUMN selected TEXT")
    # User-editable display label. NULL falls back to a basename derived
    # from source_path on the frontend.
    if "label" not in ac_cols:
        conn.execute("ALTER TABLE audio_clips ADD COLUMN label TEXT")

    # M13 + M16: pool_segments gains variant_kind, derived_from, and
    # context_entity_{type,id} columns.
    # - variant_kind: canonical flag for derived variants (M13 'lipsync',
    #   M16 'music'; future 'denoise' etc.). Candidates-tab filters to
    #   variant_kind IS NULL.
    # - derived_from: typed FK to another pool_segment for true content
    #   derivation (M13 lipsync case — output depends on source candidate's
    #   video). NOT used by M16 music gen.
    # - context_entity_{type,id}: polymorphic weak-context-provenance ref
    #   for M16 music gen (entity selected at generation time; independent
    #   of source content). See spec Q2.2 Option Y.
    ps_cols = {row[1] for row in conn.execute("PRAGMA table_info(pool_segments)").fetchall()}
    if "variant_kind" not in ps_cols:
        conn.execute("ALTER TABLE pool_segments ADD COLUMN variant_kind TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pool_segments_variant_kind ON pool_segments(variant_kind) WHERE variant_kind IS NOT NULL")
    if "derived_from" not in ps_cols:
        conn.execute("ALTER TABLE pool_segments ADD COLUMN derived_from TEXT REFERENCES pool_segments(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pool_segments_derived_from ON pool_segments(derived_from) WHERE derived_from IS NOT NULL")
    if "context_entity_type" not in ps_cols:
        conn.execute("ALTER TABLE pool_segments ADD COLUMN context_entity_type TEXT")
    if "context_entity_id" not in ps_cols:
        conn.execute("ALTER TABLE pool_segments ADD COLUMN context_entity_id TEXT")

    # ── Migration: relax track_effects.track_id to NULL (master-bus effects) ──
    # SQLite can't ALTER COLUMN to change NOT NULL, so detect the legacy schema
    # via PRAGMA table_info and rewrite the table in-place. Idempotent: the
    # check short-circuits once track_id is already nullable. All existing rows
    # keep their non-null track_id (untouched); future rows CAN pass NULL to
    # mark master-bus effects.
    te_info = conn.execute("PRAGMA table_info(track_effects)").fetchall()
    te_track_id_notnull = any(
        row[1] == "track_id" and row[3] == 1 for row in te_info
    )
    if te_track_id_notnull:
        conn.executescript("""
            CREATE TABLE track_effects__new (
                id TEXT PRIMARY KEY,
                track_id TEXT REFERENCES audio_tracks(id) ON DELETE CASCADE,
                effect_type TEXT NOT NULL,
                order_index INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                static_params TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO track_effects__new
                (id, track_id, effect_type, order_index, enabled, static_params, created_at)
                SELECT id, track_id, effect_type, order_index, enabled, static_params, created_at
                FROM track_effects;
            DROP TABLE track_effects;
            ALTER TABLE track_effects__new RENAME TO track_effects;
            CREATE INDEX IF NOT EXISTS track_effects_track_order
                ON track_effects(track_id, order_index);
        """)

    # ── Migration: add dmx_* patch columns to light_show__fixtures ──
    ls_cols = {row[1] for row in conn.execute("PRAGMA table_info(light_show__fixtures)").fetchall()}
    if "dmx_universe" not in ls_cols:
        conn.execute("ALTER TABLE light_show__fixtures ADD COLUMN dmx_universe INTEGER")
    if "dmx_address" not in ls_cols:
        conn.execute("ALTER TABLE light_show__fixtures ADD COLUMN dmx_address INTEGER")
    if "dmx_channel_count" not in ls_cols:
        conn.execute("ALTER TABLE light_show__fixtures ADD COLUMN dmx_channel_count INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_light_show_fixtures_dmx "
        "ON light_show__fixtures(dmx_universe, dmx_address)"
    )

    # ── Undo triggers (AFTER all migrations so PRAGMA table_info sees all columns) ──
    _undo_tracked_tables = ["keyframes", "transitions", "suppressions", "effects", "tracks", "transition_effects", "markers", "audio_tracks", "audio_clips", "audio_isolations", "track_effects", "effect_curves", "project_send_buses", "project_frequency_labels"]
    for table in _undo_tracked_tables:
        cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        col_names = [row[1] for row in cols_info]
        if not col_names:
            continue

        # Drop + recreate so triggers always reflect latest columns
        conn.execute(f"DROP TRIGGER IF EXISTS {table}_insert_undo")
        conn.execute(f"DROP TRIGGER IF EXISTS {table}_update_undo")
        conn.execute(f"DROP TRIGGER IF EXISTS {table}_delete_undo")

        conn.execute(f"CREATE TRIGGER {table}_insert_undo AFTER INSERT ON {table} WHEN (SELECT value FROM undo_state WHERE key='active') = 1 BEGIN INSERT INTO undo_log (undo_group, sql_text) SELECT value, 'DELETE FROM {table} WHERE id=' || quote(NEW.id) FROM undo_state WHERE key='current_group'; END;")

        set_clauses = " || ',' || ".join([f"'{col}=' || quote(OLD.{col})" for col in col_names])
        conn.execute(f"CREATE TRIGGER {table}_update_undo AFTER UPDATE ON {table} WHEN (SELECT value FROM undo_state WHERE key='active') = 1 BEGIN INSERT INTO undo_log (undo_group, sql_text) SELECT value, 'UPDATE {table} SET ' || {set_clauses} || ' WHERE id=' || quote(OLD.id) FROM undo_state WHERE key='current_group'; END;")

        col_list = ", ".join(col_names)
        val_exprs = " || ',' || ".join([f"quote(OLD.{col})" for col in col_names])
        conn.execute(f"CREATE TRIGGER {table}_delete_undo AFTER DELETE ON {table} WHEN (SELECT value FROM undo_state WHERE key='active') = 1 BEGIN INSERT INTO undo_log (undo_group, sql_text) SELECT value, 'INSERT INTO {table} ({col_list}) VALUES (' || {val_exprs} || ')' FROM undo_state WHERE key='current_group'; END;")

    # ── Composite-PK undo triggers for isolation_stems ──
    # isolation_stems has a composite PK (isolation_id, pool_segment_id) rather
    # than a single `id`, so it needs explicit trigger SQL keyed on both columns.
    conn.execute("DROP TRIGGER IF EXISTS isolation_stems_insert_undo")
    conn.execute("DROP TRIGGER IF EXISTS isolation_stems_update_undo")
    conn.execute("DROP TRIGGER IF EXISTS isolation_stems_delete_undo")
    conn.execute(
        "CREATE TRIGGER isolation_stems_insert_undo AFTER INSERT ON isolation_stems "
        "WHEN (SELECT value FROM undo_state WHERE key='active') = 1 "
        "BEGIN INSERT INTO undo_log (undo_group, sql_text) "
        "SELECT value, 'DELETE FROM isolation_stems WHERE isolation_id=' || quote(NEW.isolation_id) "
        "|| ' AND pool_segment_id=' || quote(NEW.pool_segment_id) "
        "FROM undo_state WHERE key='current_group'; END;"
    )
    conn.execute(
        "CREATE TRIGGER isolation_stems_update_undo AFTER UPDATE ON isolation_stems "
        "WHEN (SELECT value FROM undo_state WHERE key='active') = 1 "
        "BEGIN INSERT INTO undo_log (undo_group, sql_text) "
        "SELECT value, 'UPDATE isolation_stems SET isolation_id=' || quote(OLD.isolation_id) "
        "|| ',pool_segment_id=' || quote(OLD.pool_segment_id) "
        "|| ',stem_type=' || quote(OLD.stem_type) "
        "|| ' WHERE isolation_id=' || quote(OLD.isolation_id) "
        "|| ' AND pool_segment_id=' || quote(OLD.pool_segment_id) "
        "FROM undo_state WHERE key='current_group'; END;"
    )
    conn.execute(
        "CREATE TRIGGER isolation_stems_delete_undo AFTER DELETE ON isolation_stems "
        "WHEN (SELECT value FROM undo_state WHERE key='active') = 1 "
        "BEGIN INSERT INTO undo_log (undo_group, sql_text) "
        "SELECT value, 'INSERT INTO isolation_stems (isolation_id, pool_segment_id, stem_type) VALUES (' "
        "|| quote(OLD.isolation_id) || ',' || quote(OLD.pool_segment_id) || ',' || quote(OLD.stem_type) || ')' "
        "FROM undo_state WHERE key='current_group'; END;"
    )

    # ── M13 task-45: composite-PK undo triggers for track_sends ──
    # track_sends has a composite PK (track_id, bus_id); mirror the isolation_stems
    # pattern. The auto-seed trigger below runs OUTSIDE the undo_state gate so
    # that default-bus rows inserted on audio_track create are NOT themselves
    # recorded as a separate undo unit — they ride along with the parent insert.
    conn.execute("DROP TRIGGER IF EXISTS track_sends_insert_undo")
    conn.execute("DROP TRIGGER IF EXISTS track_sends_update_undo")
    conn.execute("DROP TRIGGER IF EXISTS track_sends_delete_undo")
    conn.execute(
        "CREATE TRIGGER track_sends_insert_undo AFTER INSERT ON track_sends "
        "WHEN (SELECT value FROM undo_state WHERE key='active') = 1 "
        "BEGIN INSERT INTO undo_log (undo_group, sql_text) "
        "SELECT value, 'DELETE FROM track_sends WHERE track_id=' || quote(NEW.track_id) "
        "|| ' AND bus_id=' || quote(NEW.bus_id) "
        "FROM undo_state WHERE key='current_group'; END;"
    )
    conn.execute(
        "CREATE TRIGGER track_sends_update_undo AFTER UPDATE ON track_sends "
        "WHEN (SELECT value FROM undo_state WHERE key='active') = 1 "
        "BEGIN INSERT INTO undo_log (undo_group, sql_text) "
        "SELECT value, 'UPDATE track_sends SET track_id=' || quote(OLD.track_id) "
        "|| ',bus_id=' || quote(OLD.bus_id) "
        "|| ',level=' || quote(OLD.level) "
        "|| ' WHERE track_id=' || quote(OLD.track_id) "
        "|| ' AND bus_id=' || quote(OLD.bus_id) "
        "FROM undo_state WHERE key='current_group'; END;"
    )
    conn.execute(
        "CREATE TRIGGER track_sends_delete_undo AFTER DELETE ON track_sends "
        "WHEN (SELECT value FROM undo_state WHERE key='active') = 1 "
        "BEGIN INSERT INTO undo_log (undo_group, sql_text) "
        "SELECT value, 'INSERT INTO track_sends (track_id, bus_id, level) VALUES (' "
        "|| quote(OLD.track_id) || ',' || quote(OLD.bus_id) || ',' || quote(OLD.level) || ')' "
        "FROM undo_state WHERE key='current_group'; END;"
    )

    # ── M13 task-45: auto-insert track_sends rows on audio_track INSERT ──
    # Per spec R4 + test `track-sends-row-per-track-per-bus`: every new audio
    # track must get a level=0 send row for each existing bus automatically.
    # Enforced via SQL trigger so no call-site can forget.
    conn.execute("DROP TRIGGER IF EXISTS audio_tracks_seed_sends")
    conn.execute(
        "CREATE TRIGGER audio_tracks_seed_sends AFTER INSERT ON audio_tracks "
        "BEGIN "
        "INSERT OR IGNORE INTO track_sends (track_id, bus_id, level) "
        "SELECT NEW.id, id, 0.0 FROM project_send_buses; "
        "END;"
    )

    # ── M13 task-45: seed default send buses on migration + new-project ──
    # Per spec §Behavior "Configuring Send Buses" + test
    # `send-bus-defaults-on-new-project`: 2 reverb (Plate, Hall) + 1 delay +
    # 1 echo, in that order_index. Idempotent: only seeds if the table is
    # empty, so re-runs on an existing migrated DB are no-ops.
    existing_bus_count = conn.execute("SELECT COUNT(*) FROM project_send_buses").fetchone()[0]
    if existing_bus_count == 0:
        _seed_default_send_buses(conn)
        # For any audio_tracks that already existed before this migration,
        # backfill track_sends rows to the just-seeded buses.
        conn.execute(
            "INSERT OR IGNORE INTO track_sends (track_id, bus_id, level) "
            "SELECT t.id, b.id, 0.0 FROM audio_tracks t CROSS JOIN project_send_buses b"
        )

    # Commit all DDL + seed inserts. Without this, the connection holds an open
    # transaction that blocks writes from other threads (e.g., background workers,
    # API request handlers on separate threads).
    conn.commit()


# ── Meta operations ─────────────────────────────────────────────────

def get_meta(project_dir: Path) -> dict:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT key, value FROM meta").fetchall()
    meta = {}
    for row in rows:
        try:
            meta[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            meta[row["key"]] = row["value"]
    return meta


def set_meta(project_dir: Path, key: str, value):
    conn = get_db(project_dir)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (key, json.dumps(value) if not isinstance(value, str) else value),
    )
    conn.commit()


def set_meta_bulk(project_dir: Path, meta: dict):
    conn = get_db(project_dir)
    for key, value in meta.items():
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, json.dumps(value) if not isinstance(value, str) else value),
        )
    conn.commit()


def _resolve_audio_path(project_dir: Path, meta: dict) -> str | None:
    """Find the project's audio file.

    Checks meta['audio'] first (absolute path or project-relative),
    otherwise globs the project dir for common audio extensions.
    Returns None if no audio is found.
    """
    raw = meta.get("audio")
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = project_dir / p
        if p.exists():
            return str(p)
    for ext in ("wav", "mp3", "flac", "m4a", "ogg"):
        for f in project_dir.glob(f"*.{ext}"):
            return str(f)
    return None


def load_project_data(project_dir: Path) -> dict:
    """Load all project data the renderer + generation pipeline need, from SQLite.

    Returns a dict shaped like the old load_narrative() result so callers can
    migrate without chasing down every field access. Key fields:
        - meta: dict (includes _audio_resolved, _work_dir)
        - keyframes: list[dict] (active only, with _timestamp_seconds)
        - transitions: list[dict] (active only)
        - _work_dir: str
        - _project_dir: Path
    """
    meta = get_meta(project_dir)
    kfs = [kf for kf in get_keyframes(project_dir) if not kf.get("deleted_at")]
    # Compute per-keyframe seconds for callers that rely on _timestamp_seconds
    for kf in kfs:
        ts = kf.get("timestamp", "0:00")
        parts = str(ts).split(":")
        if len(parts) == 2:
            try:
                kf["_timestamp_seconds"] = int(parts[0]) * 60 + float(parts[1])
            except ValueError:
                kf["_timestamp_seconds"] = 0.0
        else:
            kf["_timestamp_seconds"] = 0.0
    trs = [tr for tr in get_transitions(project_dir) if not tr.get("deleted_at")]

    audio = _resolve_audio_path(project_dir, meta)
    if audio:
        meta["_audio_resolved"] = audio

    return {
        "meta": meta,
        "keyframes": kfs,
        "transitions": trs,
        "_work_dir": str(project_dir),
        "_project_dir": project_dir,
    }


# ── Keyframe operations ─────────────────────────────────────────────

def _row_to_keyframe(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "section": row["section"],
        "source": row["source"],
        "prompt": row["prompt"],
        "selected": row["selected"],
        "candidates": json.loads(row["candidates"]),
        "context": json.loads(row["context"]) if row["context"] else None,
        "track_id": row["track_id"] if "track_id" in row.keys() else "track_1",
        "label": row["label"] if "label" in row.keys() else "",
        "label_color": row["label_color"] if "label_color" in row.keys() else "",
        "blend_mode": row["blend_mode"] if "blend_mode" in row.keys() else "",
        "opacity": row["opacity"] if "opacity" in row.keys() else None,
        "refinement_prompt": row["refinement_prompt"] if "refinement_prompt" in row.keys() else "",
        "deleted_at": row["deleted_at"],
    }


def get_keyframes(project_dir: Path, include_deleted: bool = False) -> list[dict]:
    conn = get_db(project_dir)
    if include_deleted:
        rows = conn.execute("SELECT * FROM keyframes ORDER BY timestamp").fetchall()
    else:
        rows = conn.execute("SELECT * FROM keyframes WHERE deleted_at IS NULL ORDER BY timestamp").fetchall()
    return [_row_to_keyframe(r) for r in rows]


def get_keyframe(project_dir: Path, kf_id: str) -> dict | None:
    conn = get_db(project_dir)
    row = conn.execute("SELECT * FROM keyframes WHERE id = ?", (kf_id,)).fetchone()
    return _row_to_keyframe(row) if row else None


def get_binned_keyframes(project_dir: Path) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT * FROM keyframes WHERE deleted_at IS NOT NULL ORDER BY timestamp").fetchall()
    return [_row_to_keyframe(r) for r in rows]


def add_keyframe(project_dir: Path, kf: dict):
    conn = get_db(project_dir)
    def _do():
        conn.execute(
            """INSERT OR REPLACE INTO keyframes (id, timestamp, section, source, prompt, selected, candidates, context, deleted_at, track_id, label, label_color, blend_mode, opacity, refinement_prompt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (kf["id"], kf["timestamp"], kf.get("section", ""), kf.get("source", ""),
             kf.get("prompt", ""), kf.get("selected"), json.dumps(kf.get("candidates", [])),
             json.dumps(kf.get("context")) if kf.get("context") else None, kf.get("deleted_at"),
             kf.get("track_id", "track_1"), kf.get("label", ""), kf.get("label_color", ""),
             kf.get("blend_mode", ""), kf.get("opacity"), kf.get("refinement_prompt", "")),
        )
        conn.commit()
    _retry_on_locked(_do)


def _parse_kf_timestamp(ts) -> float:
    """Parse 'm:ss(.fff)', 'H:MM:SS(.fff)', or a numeric timestamp to seconds.

    Returns None-like 0.0 on unparseable input — safe for delta computation
    (a zero delta is a no-op in propagation).
    """
    if isinstance(ts, (int, float)):
        return float(ts)
    if not ts:
        return 0.0
    parts = str(ts).split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return 0.0
    return 0.0


def _propagate_linked_audio_on_from_kf_shift(project_dir: Path, kf_id: str, delta: float):
    """When a keyframe's timestamp shifts by `delta`, for every transition
    where kf is `from_kf` (i.e. the transition's start), shift each linked
    audio clip's start_time and end_time by the same delta. Per the M9
    design invariant table: "Transition trimmed start by Δ → clip start
    +Δ, clip end +Δ" (which also subsumes the move case).

    Zero-delta calls are a no-op.
    """
    if abs(delta) < 1e-9:
        return
    conn = get_db(project_dir)
    # Find transitions where this kf is the start anchor
    tr_rows = conn.execute(
        "SELECT id FROM transitions WHERE from_kf = ? AND deleted_at IS NULL",
        (kf_id,),
    ).fetchall()
    if not tr_rows:
        return
    tr_ids = [r["id"] for r in tr_rows]
    # Collect all linked audio clip ids across those transitions
    placeholders = ",".join("?" for _ in tr_ids)
    link_rows = conn.execute(
        f"SELECT DISTINCT audio_clip_id FROM audio_clip_links WHERE transition_id IN ({placeholders})",
        tr_ids,
    ).fetchall()
    clip_ids = [r["audio_clip_id"] for r in link_rows]
    if not clip_ids:
        return
    # Shift each clip's start/end by delta in one pass
    clip_placeholders = ",".join("?" for _ in clip_ids)
    conn.execute(
        f"UPDATE audio_clips SET start_time = start_time + ?, end_time = end_time + ? "
        f"WHERE id IN ({clip_placeholders}) AND deleted_at IS NULL",
        [delta, delta, *clip_ids],
    )
    conn.commit()


def update_keyframe(project_dir: Path, kf_id: str, **fields):
    conn = get_db(project_dir)
    # M9 task-85: if the keyframe's timestamp is being changed, compute the
    # delta against the current stored value and propagate to linked audio
    # clips on any transition where this kf is the start anchor.
    ts_delta: float = 0.0
    if "timestamp" in fields:
        old_row = conn.execute("SELECT timestamp FROM keyframes WHERE id = ?", (kf_id,)).fetchone()
        if old_row is not None:
            old_sec = _parse_kf_timestamp(old_row["timestamp"])
            new_sec = _parse_kf_timestamp(fields["timestamp"])
            ts_delta = new_sec - old_sec

    sets = []
    values = []
    for key, val in fields.items():
        col = key
        if key == "candidates" or key == "context":
            val = json.dumps(val) if val is not None else None
        sets.append(f"{col} = ?")
        values.append(val)
    values.append(kf_id)
    _retry_on_locked(lambda: (conn.execute(f"UPDATE keyframes SET {', '.join(sets)} WHERE id = ?", values), conn.commit()))

    if ts_delta != 0.0:
        try:
            _propagate_linked_audio_on_from_kf_shift(project_dir, kf_id, ts_delta)
        except sqlite3.DatabaseError as e:
            # Don't block the main update if audio propagation fails — log and move on
            import sys as _sys
            print(f"[db.update_keyframe] linked-audio propagation failed for {kf_id} Δ={ts_delta}: {e}",
                  file=_sys.stderr, flush=True)


def delete_keyframe(project_dir: Path, kf_id: str, deleted_at: str):
    """Soft-delete a keyframe."""
    conn = get_db(project_dir)
    conn.execute("UPDATE keyframes SET deleted_at = ? WHERE id = ?", (deleted_at, kf_id))
    conn.commit()


def restore_keyframe(project_dir: Path, kf_id: str):
    conn = get_db(project_dir)
    conn.execute("UPDATE keyframes SET deleted_at = NULL WHERE id = ?", (kf_id,))
    conn.commit()


def next_keyframe_id(project_dir: Path) -> str:
    return generate_id("kf")


# ── Transition operations ───────────────────────────────────────────

def _row_to_transition(row: sqlite3.Row) -> dict:
    remap = json.loads(row["remap"]) if row["remap"] else {"method": "linear", "target_duration": 0}
    selected_raw = json.loads(row["selected"]) if row["selected"] else [None]
    # Flatten legacy [N] to N for frontend compat
    selected = selected_raw[0] if isinstance(selected_raw, list) and len(selected_raw) == 1 else selected_raw
    return {
        "id": row["id"],
        "from": row["from_kf"],
        "to": row["to_kf"],
        "duration_seconds": row["duration_seconds"],
        "slots": row["slots"],
        "action": row["action"],
        "use_global_prompt": bool(row["use_global_prompt"]),
        "selected": selected,
        "remap": remap,
        "track_id": row["track_id"] if "track_id" in row.keys() else "track_1",
        "label": row["label"] if "label" in row.keys() else "",
        "label_color": row["label_color"] if "label_color" in row.keys() else "",
        "tags": json.loads(row["tags"]) if "tags" in row.keys() and row["tags"] else [],
        "blend_mode": row["blend_mode"] if "blend_mode" in row.keys() else "",
        "opacity": row["opacity"] if "opacity" in row.keys() else None,
        "opacity_curve": json.loads(row["opacity_curve"]) if "opacity_curve" in row.keys() and row["opacity_curve"] else None,
        "saturation_curve": json.loads(row["saturation_curve"]) if "saturation_curve" in row.keys() and row["saturation_curve"] else None,
        "red_curve": json.loads(row["red_curve"]) if "red_curve" in row.keys() and row["red_curve"] else None,
        "green_curve": json.loads(row["green_curve"]) if "green_curve" in row.keys() and row["green_curve"] else None,
        "blue_curve": json.loads(row["blue_curve"]) if "blue_curve" in row.keys() and row["blue_curve"] else None,
        "black_curve": json.loads(row["black_curve"]) if "black_curve" in row.keys() and row["black_curve"] else None,
        "hue_shift_curve": json.loads(row["hue_shift_curve"]) if "hue_shift_curve" in row.keys() and row["hue_shift_curve"] else None,
        "invert_curve": json.loads(row["invert_curve"]) if "invert_curve" in row.keys() and row["invert_curve"] else None,
        "brightness_curve": json.loads(row["brightness_curve"]) if "brightness_curve" in row.keys() and row["brightness_curve"] else None,
        "contrast_curve": json.loads(row["contrast_curve"]) if "contrast_curve" in row.keys() and row["contrast_curve"] else None,
        "exposure_curve": json.loads(row["exposure_curve"]) if "exposure_curve" in row.keys() and row["exposure_curve"] else None,
        "chroma_key": json.loads(row["chroma_key"]) if "chroma_key" in row.keys() and row["chroma_key"] else None,
        "is_adjustment": bool(row["is_adjustment"]) if "is_adjustment" in row.keys() else False,
        "mask_center_x": row["mask_center_x"] if "mask_center_x" in row.keys() else None,
        "mask_center_y": row["mask_center_y"] if "mask_center_y" in row.keys() else None,
        "mask_radius": row["mask_radius"] if "mask_radius" in row.keys() else None,
        "mask_feather": row["mask_feather"] if "mask_feather" in row.keys() else None,
        "transform_x": row["transform_x"] if "transform_x" in row.keys() else None,
        "transform_y": row["transform_y"] if "transform_y" in row.keys() else None,
        "transform_x_curve": json.loads(row["transform_x_curve"]) if "transform_x_curve" in row.keys() and row["transform_x_curve"] else None,
        "transform_y_curve": json.loads(row["transform_y_curve"]) if "transform_y_curve" in row.keys() and row["transform_y_curve"] else None,
        "transform_scale_x_curve": json.loads(row["transform_scale_x_curve"]) if "transform_scale_x_curve" in row.keys() and row["transform_scale_x_curve"] else None,
        "transform_scale_y_curve": json.loads(row["transform_scale_y_curve"]) if "transform_scale_y_curve" in row.keys() and row["transform_scale_y_curve"] else None,
        "anchor_x": row["anchor_x"] if "anchor_x" in row.keys() else None,
        "anchor_y": row["anchor_y"] if "anchor_y" in row.keys() else None,
        "deleted_at": row["deleted_at"],
        "include_section_desc": bool(row["include_section_desc"]) if "include_section_desc" in row.keys() else True,
        "hidden": bool(row["hidden"]) if "hidden" in row.keys() else False,
        "ingredients": json.loads(row["ingredients"]) if "ingredients" in row.keys() and row["ingredients"] else [],
        "negativePrompt": row["negative_prompt"] if "negative_prompt" in row.keys() else "",
        "seed": row["seed"] if "seed" in row.keys() else None,
        "trim_in": row["trim_in"] if "trim_in" in row.keys() else 0.0,
        "trim_out": row["trim_out"] if "trim_out" in row.keys() else None,
        "source_video_duration": row["source_video_duration"] if "source_video_duration" in row.keys() else None,
    }


def get_transitions(project_dir: Path, include_deleted: bool = False) -> list[dict]:
    conn = get_db(project_dir)
    if include_deleted:
        rows = conn.execute("SELECT * FROM transitions").fetchall()
    else:
        rows = conn.execute("SELECT * FROM transitions WHERE deleted_at IS NULL").fetchall()
    return [_row_to_transition(r) for r in rows]


def get_transition(project_dir: Path, tr_id: str) -> dict | None:
    conn = get_db(project_dir)
    row = conn.execute("SELECT * FROM transitions WHERE id = ?", (tr_id,)).fetchone()
    return _row_to_transition(row) if row else None


def get_binned_transitions(project_dir: Path) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT * FROM transitions WHERE deleted_at IS NOT NULL").fetchall()
    return [_row_to_transition(r) for r in rows]


def add_transition(project_dir: Path, tr: dict):
    conn = get_db(project_dir)
    selected = tr.get("selected")
    if isinstance(selected, (int, str)) and selected is not None:
        selected = [selected]
    elif selected is None:
        selected = [None]
    # Derive track_id from the 'from' keyframe if not explicitly provided
    track_id = tr.get("track_id")
    if not track_id:
        from_kf = tr.get("from", "")
        if from_kf:
            row = conn.execute("SELECT track_id FROM keyframes WHERE id = ?", (from_kf,)).fetchone()
            track_id = row["track_id"] if row else "track_1"
        else:
            track_id = "track_1"
    def _json_or_none(val):
        return json.dumps(val) if isinstance(val, list) else val

    def _do_insert():
        conn.execute(
            """INSERT OR REPLACE INTO transitions (id, from_kf, to_kf, duration_seconds, slots, action, use_global_prompt, selected, remap, deleted_at, track_id, label, label_color, tags, blend_mode, opacity, opacity_curve, red_curve, green_curve, blue_curve, black_curve, hue_shift_curve, saturation_curve, invert_curve, is_adjustment, mask_center_x, mask_center_y, mask_radius, mask_feather, transform_x, transform_y, transform_x_curve, transform_y_curve, transform_scale_x_curve, transform_scale_y_curve, hidden, anchor_x, anchor_y)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tr["id"], tr.get("from", ""), tr.get("to", ""), tr.get("duration_seconds", 0),
             tr.get("slots", 1), tr.get("action", ""), int(tr.get("use_global_prompt", False)),
             json.dumps(selected), json.dumps(tr.get("remap", {"method": "linear", "target_duration": 0})),
             tr.get("deleted_at"), track_id,
             tr.get("label", ""), tr.get("label_color", ""),
             json.dumps(tr.get("tags", [])) if isinstance(tr.get("tags"), list) else tr.get("tags", "[]"),
             tr.get("blend_mode", ""), tr.get("opacity"),
             _json_or_none(tr.get("opacity_curve")),
             _json_or_none(tr.get("red_curve")),
             _json_or_none(tr.get("green_curve")),
             _json_or_none(tr.get("blue_curve")),
             _json_or_none(tr.get("black_curve")),
             _json_or_none(tr.get("hue_shift_curve")),
             _json_or_none(tr.get("saturation_curve")),
             _json_or_none(tr.get("invert_curve")),
             int(tr.get("is_adjustment", False)),
             tr.get("mask_center_x"), tr.get("mask_center_y"), tr.get("mask_radius"), tr.get("mask_feather"),
             tr.get("transform_x"), tr.get("transform_y"),
             _json_or_none(tr.get("transform_x_curve")),
             _json_or_none(tr.get("transform_y_curve")),
             _json_or_none(tr.get("transform_scale_x_curve")),
             _json_or_none(tr.get("transform_scale_y_curve")),
             int(tr.get("hidden", False)),
             tr.get("anchor_x"), tr.get("anchor_y")),
        )
        conn.commit()
    _retry_on_locked(_do_insert)


def update_transition(project_dir: Path, tr_id: str, **fields):
    conn = get_db(project_dir)
    sets = []
    values = []
    for key, val in fields.items():
        col = key
        if key == "from":
            col = "from_kf"
        elif key == "to":
            col = "to_kf"
        elif key == "selected":
            if isinstance(val, (int, str)) and val is not None:
                val = json.dumps([val])
            elif val is None:
                val = json.dumps([None])
            else:
                val = json.dumps(val)
        elif key == "remap":
            val = json.dumps(val)
        elif key == "use_global_prompt":
            val = int(val)
        elif key == "include_section_desc":
            val = int(val)
        elif key == "is_adjustment":
            val = int(val or 0)
        elif key == "hidden":
            val = int(val or 0)
        elif key == "tags":
            val = json.dumps(val) if isinstance(val, list) else val
        elif key in ("opacity_curve", "red_curve", "green_curve", "blue_curve", "black_curve", "hue_shift_curve", "saturation_curve", "invert_curve", "brightness_curve", "contrast_curve", "exposure_curve", "transform_x_curve", "transform_y_curve", "transform_scale_x_curve", "transform_scale_y_curve"):
            val = json.dumps(val) if isinstance(val, list) else val
        elif key == "chroma_key":
            val = json.dumps(val) if isinstance(val, (dict, list)) else val
        elif key == "ingredients":
            val = json.dumps(val) if isinstance(val, list) else val
        sets.append(f"{col} = ?")
        values.append(val)
    if not sets:
        return
    values.append(tr_id)
    _retry_on_locked(lambda: (conn.execute(f"UPDATE transitions SET {', '.join(sets)} WHERE id = ?", values), conn.commit()))


def delete_transition(project_dir: Path, tr_id: str, deleted_at: str):
    """Soft-delete a transition. Per M9 task-86, also soft-deletes every
    linked audio clip and removes the link rows. Undo restores both.
    """
    conn = get_db(project_dir)
    # Collect linked audio clip ids before we drop the links
    link_rows = conn.execute(
        "SELECT audio_clip_id FROM audio_clip_links WHERE transition_id = ?",
        (tr_id,),
    ).fetchall()
    linked_clip_ids = [r["audio_clip_id"] for r in link_rows]

    conn.execute("UPDATE transitions SET deleted_at = ? WHERE id = ?", (deleted_at, tr_id))
    if linked_clip_ids:
        placeholders = ",".join("?" for _ in linked_clip_ids)
        conn.execute(
            f"UPDATE audio_clips SET deleted_at = ? WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            [deleted_at, *linked_clip_ids],
        )
        conn.execute("DELETE FROM audio_clip_links WHERE transition_id = ?", (tr_id,))
    conn.commit()


def restore_transition(project_dir: Path, tr_id: str):
    conn = get_db(project_dir)
    conn.execute("UPDATE transitions SET deleted_at = NULL WHERE id = ?", (tr_id,))
    conn.commit()


# ── Transition effects ─────────────────────────────────────────────

def get_transition_effects(project_dir: Path, transition_id: str) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM transition_effects WHERE transition_id = ? ORDER BY z_order",
        (transition_id,),
    ).fetchall()
    return [{"id": r["id"], "transitionId": r["transition_id"], "type": r["type"],
             "params": json.loads(r["params"]), "enabled": bool(r["enabled"]), "zOrder": r["z_order"]} for r in rows]


def get_all_transition_effects(project_dir: Path) -> dict[str, list[dict]]:
    """Returns a dict mapping transition_id -> list of effects."""
    conn = get_db(project_dir)
    rows = conn.execute("SELECT * FROM transition_effects ORDER BY z_order").fetchall()
    result: dict[str, list[dict]] = {}
    for r in rows:
        tr_id = r["transition_id"]
        if tr_id not in result:
            result[tr_id] = []
        result[tr_id].append({"id": r["id"], "transitionId": tr_id, "type": r["type"],
                              "params": json.loads(r["params"]), "enabled": bool(r["enabled"]), "zOrder": r["z_order"]})
    return result


def add_transition_effect(project_dir: Path, transition_id: str, effect_type: str, params: dict | None = None) -> str:
    conn = get_db(project_dir)
    effect_id = generate_id("tfx")
    max_z = conn.execute("SELECT COALESCE(MAX(z_order), -1) FROM transition_effects WHERE transition_id = ?", (transition_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO transition_effects (id, transition_id, type, params, enabled, z_order) VALUES (?, ?, ?, ?, 1, ?)",
        (effect_id, transition_id, effect_type, json.dumps(params or {}), max_z + 1),
    )
    conn.commit()
    return effect_id


def update_transition_effect(project_dir: Path, effect_id: str, **fields):
    conn = get_db(project_dir)
    sets = []
    values = []
    for key, val in fields.items():
        if key == "params":
            val = json.dumps(val) if isinstance(val, dict) else val
        elif key == "enabled":
            val = int(val)
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(effect_id)
    conn.execute(f"UPDATE transition_effects SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_transition_effect(project_dir: Path, effect_id: str):
    conn = get_db(project_dir)
    conn.execute("DELETE FROM transition_effects WHERE id = ?", (effect_id,))
    conn.commit()


def next_transition_id(project_dir: Path) -> str:
    return generate_id("tr")


def get_transitions_involving(project_dir: Path, kf_id: str) -> list[dict]:
    """Get all active transitions that reference a keyframe as from or to."""
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM transitions WHERE deleted_at IS NULL AND (from_kf = ? OR to_kf = ?)",
        (kf_id, kf_id),
    ).fetchall()
    return [_row_to_transition(r) for r in rows]


def _probe_video_duration(video_path: Path) -> float | None:
    """Return duration (seconds) for a video via ffprobe, or None on failure."""
    import subprocess as _sp
    try:
        p = _sp.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
        s = (p.stdout or "").strip()
        return float(s) if s else None
    except (ValueError, FileNotFoundError, _sp.TimeoutExpired):
        return None


def backfill_transition_trim(project_dir: Path, *, verbose: bool = False) -> dict:
    """Probe selected videos and initialize trim_in/trim_out/source_video_duration.

    Idempotent: rows where source_video_duration IS NOT NULL are skipped.
    Returns {"probed": N, "skipped": N, "missing": N}.
    """
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT id, source_video_duration FROM transitions WHERE deleted_at IS NULL"
    ).fetchall()
    probed = skipped = missing = 0
    for row in rows:
        tr_id = row["id"]
        if row["source_video_duration"] is not None:
            skipped += 1
            continue
        video_path = project_dir / "selected_transitions" / f"{tr_id}_slot_0.mp4"
        if not video_path.exists():
            missing += 1
            continue
        dur = _probe_video_duration(video_path)
        if dur is None or dur <= 0:
            missing += 1
            continue
        _retry_on_locked(lambda: (
            conn.execute(
                "UPDATE transitions SET source_video_duration = ?, trim_in = 0, trim_out = ? WHERE id = ?",
                (dur, dur, tr_id),
            ),
            conn.commit(),
        ))
        probed += 1
        if verbose:
            print(f"[backfill] {tr_id}: source={dur:.2f}s, trim=[0, {dur:.2f}]")
    return {"probed": probed, "skipped": skipped, "missing": missing}


# ── Effects / Suppressions ──────────────────────────────────────────

def get_effects(project_dir: Path) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT * FROM effects ORDER BY time").fetchall()
    return [{"id": r["id"], "type": r["type"], "time": r["time"],
             "intensity": r["intensity"], "duration": r["duration"]} for r in rows]


def save_effects(project_dir: Path, effects: list[dict], suppressions: list[dict]):
    conn = get_db(project_dir)
    conn.execute("DELETE FROM effects")
    conn.execute("DELETE FROM suppressions")
    for fx in effects:
        conn.execute(
            "INSERT INTO effects (id, type, time, intensity, duration) VALUES (?, ?, ?, ?, ?)",
            (fx["id"], fx["type"], fx["time"], fx["intensity"], fx["duration"]),
        )
    for sup in suppressions:
        conn.execute(
            "INSERT INTO suppressions (id, from_time, to_time, effect_types, layer_effect_types) VALUES (?, ?, ?, ?, ?)",
            (sup["id"], sup["from"], sup["to"],
             json.dumps(sup.get("effectTypes")) if sup.get("effectTypes") else None,
             json.dumps(sup.get("layerEffectTypes")) if sup.get("layerEffectTypes") else None),
        )
    conn.commit()


def get_suppressions(project_dir: Path) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT * FROM suppressions ORDER BY from_time").fetchall()
    return [{"id": r["id"], "from": r["from_time"], "to": r["to_time"],
             "effectTypes": json.loads(r["effect_types"]) if r["effect_types"] else None,
             "layerEffectTypes": json.loads(r["layer_effect_types"]) if "layer_effect_types" in r.keys() and r["layer_effect_types"] else None}
            for r in rows]


# ── Bench operations ─────────────────────────────────────────────────

def get_bench(project_dir: Path) -> list[dict]:
    """Get all benched items with usage tracking."""
    conn = get_db(project_dir)
    rows = conn.execute("SELECT * FROM bench ORDER BY added_at DESC").fetchall()

    # Build usage map: source_path -> list of (entity_id, timestamp)
    # Check active transitions for matching selected video paths
    active_trs = conn.execute(
        "SELECT id, from_kf, to_kf FROM transitions WHERE deleted_at IS NULL"
    ).fetchall()
    active_kfs = conn.execute(
        "SELECT id, timestamp, source FROM keyframes WHERE deleted_at IS NULL"
    ).fetchall()

    # For transitions, the source path is selected_transitions/{id}_slot_0.mp4
    # For keyframes, it's the source field or selected_keyframes/{id}.png
    tr_sources = {}
    for tr in active_trs:
        tr_path = f"selected_transitions/{tr['id']}_slot_0.mp4"
        tr_sources[tr_path] = tr
    kf_sources = {}
    for kf in active_kfs:
        kf_path = f"selected_keyframes/{kf['id']}.png"
        kf_sources[kf_path] = kf
        if kf["source"]:
            kf_sources[kf["source"]] = kf

    items = []
    for row in rows:
        src = row["source_path"]
        usages = []

        if row["type"] == "transition":
            # Find all transitions that use this video (by checking if the file content matches)
            # Simple heuristic: check if any transition's selected video path matches
            for tr_path, tr in tr_sources.items():
                # A bench item's source could be a pool path or a selected_transitions path
                # We track usage by the bench item's source_path appearing as a candidate
                pass
            # For now, scan transition candidates dirs for this source
            # This is expensive — we'll optimize later with a source_hash column
        elif row["type"] == "keyframe":
            for kf_path, kf in kf_sources.items():
                if kf["source"] == src:
                    usages.append({"entityId": kf["id"], "timestamp": kf["timestamp"]})

        items.append({
            "id": row["id"],
            "type": row["type"],
            "sourcePath": row["source_path"],
            "label": row["label"],
            "addedAt": row["added_at"],
            "usageCount": len(usages),
            "usages": usages,
        })

    return items


def add_to_bench(project_dir: Path, bench_type: str, source_path: str, label: str = "") -> str:
    """Add an item to the bench. Returns the bench ID."""
    conn = get_db(project_dir)
    from datetime import datetime, timezone
    import uuid

    bench_id = f"bench_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bench (id, type, source_path, label, added_at) VALUES (?, ?, ?, ?, ?)",
        (bench_id, bench_type, source_path, label, now),
    )
    conn.commit()
    return bench_id


def remove_from_bench(project_dir: Path, bench_id: str):
    conn = get_db(project_dir)
    conn.execute("DELETE FROM bench WHERE id = ?", (bench_id,))
    conn.commit()


def get_bench_item(project_dir: Path, bench_id: str) -> dict | None:
    conn = get_db(project_dir)
    row = conn.execute("SELECT * FROM bench WHERE id = ?", (bench_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"], "type": row["type"],
        "sourcePath": row["source_path"], "label": row["label"],
        "addedAt": row["added_at"],
    }


# ── Candidate pool operations ──────────────────────────────────────

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()


def _row_to_pool_segment(row) -> dict:
    return {
        "id": row["id"],
        "poolPath": row["pool_path"],
        "kind": row["kind"],
        "createdBy": row["created_by"],
        "originalFilename": row["original_filename"],
        "originalFilepath": row["original_filepath"],
        "label": row["label"],
        "generationParams": json.loads(row["generation_params"]) if row["generation_params"] else None,
        "createdAt": row["created_at"],
        "durationSeconds": row["duration_seconds"],
        "width": row["width"],
        "height": row["height"],
        "byteSize": row["byte_size"],
    }


def add_pool_segment(
    project_dir: Path,
    *,
    kind: str,
    created_by: str,
    pool_path: str,
    original_filename: str | None = None,
    original_filepath: str | None = None,
    label: str = "",
    generation_params: dict | None = None,
    duration_seconds: float | None = None,
    width: int | None = None,
    height: int | None = None,
    byte_size: int | None = None,
) -> str:
    """Insert a pool_segments row. Returns the generated UUID id."""
    assert kind in ("generated", "imported"), f"bad kind: {kind}"
    seg_id = uuid.uuid4().hex
    conn = get_db(project_dir)
    conn.execute(
        """INSERT INTO pool_segments
           (id, pool_path, kind, created_by, original_filename, original_filepath,
            label, generation_params, created_at, duration_seconds, width, height, byte_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (seg_id, pool_path, kind, created_by, original_filename, original_filepath,
         label, json.dumps(generation_params) if generation_params else None,
         _now_iso(), duration_seconds, width, height, byte_size),
    )
    conn.commit()
    return seg_id


def get_pool_segment(project_dir: Path, seg_id: str) -> dict | None:
    conn = get_db(project_dir)
    row = conn.execute("SELECT * FROM pool_segments WHERE id = ?", (seg_id,)).fetchone()
    return _row_to_pool_segment(row) if row else None


def list_pool_segments(project_dir: Path, kind: str | None = None) -> list[dict]:
    conn = get_db(project_dir)
    if kind:
        rows = conn.execute(
            "SELECT * FROM pool_segments WHERE kind = ? ORDER BY created_at DESC", (kind,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM pool_segments ORDER BY created_at DESC").fetchall()
    return [_row_to_pool_segment(r) for r in rows]


def update_pool_segment_label(project_dir: Path, seg_id: str, label: str) -> None:
    conn = get_db(project_dir)
    conn.execute("UPDATE pool_segments SET label = ? WHERE id = ?", (label, seg_id))
    conn.commit()


def delete_pool_segment(project_dir: Path, seg_id: str) -> None:
    """Hard-delete. Caller is responsible for verifying no tr_candidates references exist
    and for deleting the on-disk file."""
    conn = get_db(project_dir)
    conn.execute("DELETE FROM pool_segment_tags WHERE pool_segment_id = ?", (seg_id,))
    conn.execute("DELETE FROM pool_segments WHERE id = ?", (seg_id,))
    conn.commit()


# ── Pool segment tags ─────────────────────────────────────────────

def add_pool_segment_tag(project_dir: Path, seg_id: str, tag: str, tagged_by: str) -> None:
    """Idempotent — same (seg, tag) is a no-op."""
    conn = get_db(project_dir)
    conn.execute(
        "INSERT OR IGNORE INTO pool_segment_tags (pool_segment_id, tag, tagged_by, tagged_at) VALUES (?, ?, ?, ?)",
        (seg_id, tag, tagged_by, _now_iso()),
    )
    conn.commit()


def remove_pool_segment_tag(project_dir: Path, seg_id: str, tag: str) -> None:
    conn = get_db(project_dir)
    conn.execute(
        "DELETE FROM pool_segment_tags WHERE pool_segment_id = ? AND tag = ?",
        (seg_id, tag),
    )
    conn.commit()


def get_pool_segment_tags(project_dir: Path, seg_id: str) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT tag, tagged_by, tagged_at FROM pool_segment_tags WHERE pool_segment_id = ? ORDER BY tagged_at",
        (seg_id,),
    ).fetchall()
    return [{"tag": r["tag"], "taggedBy": r["tagged_by"], "taggedAt": r["tagged_at"]} for r in rows]


def list_all_tags(project_dir: Path) -> list[dict]:
    """Returns distinct tags in use with their usage counts."""
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT tag, COUNT(*) as count FROM pool_segment_tags GROUP BY tag ORDER BY count DESC"
    ).fetchall()
    return [{"tag": r["tag"], "count": r["count"]} for r in rows]


def find_segments_by_tag(project_dir: Path, tag: str) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute(
        """SELECT ps.* FROM pool_segments ps
           JOIN pool_segment_tags pst ON pst.pool_segment_id = ps.id
           WHERE pst.tag = ?
           ORDER BY ps.created_at DESC""",
        (tag,),
    ).fetchall()
    return [_row_to_pool_segment(r) for r in rows]


# ── Transition → candidate junction ────────────────────────────────

def add_tr_candidate(
    project_dir: Path,
    *,
    transition_id: str,
    slot: int,
    pool_segment_id: str,
    source: str,
    added_at: str | None = None,
) -> None:
    """Insert a junction row. If added_at is None, uses now(). Idempotent by PK."""
    assert source in ("generated", "imported", "split-inherit", "cross-tr-copy"), f"bad source: {source}"
    conn = get_db(project_dir)
    conn.execute(
        """INSERT OR IGNORE INTO tr_candidates
           (transition_id, slot, pool_segment_id, added_at, source)
           VALUES (?, ?, ?, ?, ?)""",
        (transition_id, slot, pool_segment_id, added_at or _now_iso(), source),
    )
    conn.commit()


def remove_tr_candidate(project_dir: Path, transition_id: str, slot: int, pool_segment_id: str) -> None:
    conn = get_db(project_dir)
    conn.execute(
        "DELETE FROM tr_candidates WHERE transition_id = ? AND slot = ? AND pool_segment_id = ?",
        (transition_id, slot, pool_segment_id),
    )
    conn.commit()


def get_tr_candidates(project_dir: Path, transition_id: str, slot: int = 0) -> list[dict]:
    """Return candidate rows for (tr, slot) joined with pool_segments, ordered by added_at.

    This is the ordered list that drives the v1/v2/v3 UI — callers can enumerate
    with a 1-based index to derive the display rank.
    """
    conn = get_db(project_dir)
    rows = conn.execute(
        """SELECT tc.added_at, tc.source,
                  ps.*
           FROM tr_candidates tc
           JOIN pool_segments ps ON ps.id = tc.pool_segment_id
           WHERE tc.transition_id = ? AND tc.slot = ?
           ORDER BY tc.added_at ASC""",
        (transition_id, slot),
    ).fetchall()
    result = []
    for row in rows:
        seg = _row_to_pool_segment(row)
        seg["addedAt"] = row["added_at"]
        seg["junctionSource"] = row["source"]
        result.append(seg)
    return result


def clone_tr_candidates(
    project_dir: Path,
    *,
    source_transition_id: str,
    target_transition_id: str,
    new_source: str = "split-inherit",
) -> int:
    """Clone all junction rows from source tr to target tr, preserving slot and added_at.

    Returns the count of rows cloned. Used for split + duplicate operations.
    """
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT slot, pool_segment_id, added_at FROM tr_candidates WHERE transition_id = ?",
        (source_transition_id,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """INSERT OR IGNORE INTO tr_candidates
               (transition_id, slot, pool_segment_id, added_at, source)
               VALUES (?, ?, ?, ?, ?)""",
            (target_transition_id, row["slot"], row["pool_segment_id"], row["added_at"], new_source),
        )
    conn.commit()
    return len(rows)


def count_tr_candidate_refs(project_dir: Path, pool_segment_id: str) -> int:
    """Count how many junction rows reference this pool segment. Used for GC preview."""
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT COUNT(*) as n FROM tr_candidates WHERE pool_segment_id = ?", (pool_segment_id,),
    ).fetchone()
    return row["n"] if row else 0


def find_gc_candidates(project_dir: Path) -> list[dict]:
    """Find pool_segments rows with kind='generated' that no junction row references.

    kind='imported' is never garbage-collected (user assets stay in the pool).
    """
    conn = get_db(project_dir)
    rows = conn.execute(
        """SELECT ps.* FROM pool_segments ps
           LEFT JOIN tr_candidates tc ON tc.pool_segment_id = ps.id
           WHERE ps.kind = 'generated' AND tc.pool_segment_id IS NULL""",
    ).fetchall()
    return [_row_to_pool_segment(r) for r in rows]


# ── Audio clip → candidate junction (M11) ──────────────────────────

_AUDIO_CANDIDATE_SOURCES = ("generated", "imported", "chat_generation", "plugin")


def add_audio_candidate(
    project_dir: Path,
    *,
    audio_clip_id: str,
    pool_segment_id: str,
    source: str,
    added_at: str | None = None,
) -> None:
    """Insert an audio_candidates junction row. Idempotent on (clip, segment) PK."""
    assert source in _AUDIO_CANDIDATE_SOURCES, f"bad source: {source}"
    conn = get_db(project_dir)
    conn.execute(
        """INSERT OR IGNORE INTO audio_candidates
           (audio_clip_id, pool_segment_id, added_at, source)
           VALUES (?, ?, ?, ?)""",
        (audio_clip_id, pool_segment_id, added_at or _now_iso(), source),
    )
    conn.commit()


def get_audio_candidates(project_dir: Path, audio_clip_id: str) -> list[dict]:
    """Return candidate rows for an audio clip joined with pool_segments.

    Newest first (ORDER BY added_at DESC). Each dict has the standard
    pool_segment fields plus `addedAt` and `junctionSource`.
    """
    conn = get_db(project_dir)
    rows = conn.execute(
        """SELECT ac.added_at, ac.source, ps.*
           FROM audio_candidates ac
           JOIN pool_segments ps ON ps.id = ac.pool_segment_id
           WHERE ac.audio_clip_id = ?
           ORDER BY ac.added_at DESC""",
        (audio_clip_id,),
    ).fetchall()
    result = []
    for row in rows:
        seg = _row_to_pool_segment(row)
        seg["addedAt"] = row["added_at"]
        seg["junctionSource"] = row["source"]
        result.append(seg)
    return result


def assign_audio_candidate(
    project_dir: Path, audio_clip_id: str, pool_segment_id: str | None
) -> None:
    """Set audio_clips.selected. Pass None to revert to the original source file."""
    conn = get_db(project_dir)
    conn.execute(
        "UPDATE audio_clips SET selected = ? WHERE id = ?",
        (pool_segment_id, audio_clip_id),
    )
    conn.commit()


def remove_audio_candidate(
    project_dir: Path, audio_clip_id: str, pool_segment_id: str
) -> None:
    """Delete the junction row. If the removed segment was the selected one,
    clear audio_clips.selected so playback falls back to source_path."""
    conn = get_db(project_dir)
    conn.execute(
        "DELETE FROM audio_candidates WHERE audio_clip_id = ? AND pool_segment_id = ?",
        (audio_clip_id, pool_segment_id),
    )
    conn.execute(
        "UPDATE audio_clips SET selected = NULL WHERE id = ? AND selected = ?",
        (audio_clip_id, pool_segment_id),
    )
    conn.commit()


def get_audio_clip_effective_path(project_dir: Path, audio_clip: dict) -> str:
    """Return the pool_segment's pool_path if a candidate is selected,
    otherwise the clip's source_path. Used by playback/export to honor
    the user's chosen variant transparently."""
    selected = audio_clip.get("selected")
    if selected:
        seg = get_pool_segment(project_dir, selected)
        if seg and seg.get("poolPath"):
            return seg["poolPath"]
    return audio_clip.get("source_path", "")


# ── Audio isolation runs (M11 task-100b) ───────────────────────────

_ISOLATION_ENTITY_TYPES = ("audio_clip", "transition")
_ISOLATION_RANGE_MODES = ("full", "subset")
_ISOLATION_STATUSES = ("pending", "running", "completed", "failed")


def add_audio_isolation(
    project_dir: Path,
    *,
    entity_type: str,
    entity_id: str,
    model: str,
    range_mode: str,
    trim_in: float | None,
    trim_out: float | None,
) -> str:
    """Insert a new audio_isolations row in status='pending'.

    Returns the generated isolation_id. Undo triggers will capture this insert
    automatically when called inside an undo group.
    """
    assert entity_type in _ISOLATION_ENTITY_TYPES, f"bad entity_type: {entity_type}"
    assert range_mode in _ISOLATION_RANGE_MODES, f"bad range_mode: {range_mode}"
    isolation_id = generate_id("iso")
    conn = get_db(project_dir)

    def _do():
        conn.execute(
            """INSERT INTO audio_isolations
               (id, entity_type, entity_id, model, range_mode, trim_in, trim_out,
                status, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)""",
            (isolation_id, entity_type, entity_id, model, range_mode,
             trim_in, trim_out, _now_iso()),
        )
        conn.commit()

    _retry_on_locked(_do)
    return isolation_id


def update_audio_isolation_status(
    project_dir: Path,
    isolation_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Transition an isolation run's status: pending → running → completed | failed.

    ``error`` is stored verbatim when provided (typically on 'failed'); passing
    None clears any previously-stored error message.
    """
    assert status in _ISOLATION_STATUSES, f"bad status: {status}"
    conn = get_db(project_dir)

    def _do():
        conn.execute(
            "UPDATE audio_isolations SET status = ?, error = ? WHERE id = ?",
            (status, error, isolation_id),
        )
        conn.commit()

    _retry_on_locked(_do)


def add_isolation_stem(
    project_dir: Path,
    isolation_id: str,
    pool_segment_id: str,
    stem_type: str,
) -> None:
    """Insert a junction row linking an isolation run to an emitted stem.

    Idempotent: a duplicate (isolation_id, pool_segment_id) pair is a no-op
    via ``INSERT OR IGNORE`` so retries and re-ingests don't double-insert.
    ``stem_type`` is kept open-ended in the schema but the MVP callers use
    'vocal' or 'background'.
    """
    conn = get_db(project_dir)

    def _do():
        conn.execute(
            """INSERT OR IGNORE INTO isolation_stems
               (isolation_id, pool_segment_id, stem_type)
               VALUES (?, ?, ?)""",
            (isolation_id, pool_segment_id, stem_type),
        )
        conn.commit()

    _retry_on_locked(_do)


def get_isolations_for_entity(
    project_dir: Path, entity_type: str, entity_id: str,
) -> list[dict]:
    """Return all isolation runs for an entity, newest first, with their stems.

    Each run dict shape::

        {
          "id": str, "status": str, "model": str, "range_mode": str,
          "trim_in": float | None, "trim_out": float | None, "error": str | None,
          "created_at": str,
          "stems": [
            {"pool_segment_id": str, "stem_type": str,
             "pool_path": str, "duration_seconds": float | None}, ...
          ],
        }
    """
    conn = get_db(project_dir)

    def _do():
        run_rows = conn.execute(
            """SELECT id, entity_type, entity_id, model, range_mode, trim_in, trim_out,
                      status, error, created_at
               FROM audio_isolations
               WHERE entity_type = ? AND entity_id = ?
               ORDER BY created_at DESC""",
            (entity_type, entity_id),
        ).fetchall()
        results: list[dict] = []
        for row in run_rows:
            stem_rows = conn.execute(
                """SELECT s.pool_segment_id, s.stem_type,
                          ps.pool_path, ps.duration_seconds
                   FROM isolation_stems s
                   JOIN pool_segments ps ON ps.id = s.pool_segment_id
                   WHERE s.isolation_id = ?
                   ORDER BY s.stem_type""",
                (row["id"],),
            ).fetchall()
            stems = [
                {
                    "pool_segment_id": sr["pool_segment_id"],
                    "stem_type": sr["stem_type"],
                    "pool_path": sr["pool_path"],
                    "duration_seconds": sr["duration_seconds"],
                }
                for sr in stem_rows
            ]
            results.append({
                "id": row["id"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "model": row["model"],
                "range_mode": row["range_mode"],
                "trim_in": row["trim_in"],
                "trim_out": row["trim_out"],
                "status": row["status"],
                "error": row["error"],
                "created_at": row["created_at"],
                "stems": stems,
            })
        return results

    return _retry_on_locked(_do)


def get_isolation_stems(project_dir: Path, isolation_id: str) -> list[dict]:
    """Return stems for a single isolation run, joined with pool_segments.

    Each entry::

        {"pool_segment_id": str, "stem_type": str,
         "pool_path": str, "duration_seconds": float | None}
    """
    conn = get_db(project_dir)

    def _do():
        rows = conn.execute(
            """SELECT s.pool_segment_id, s.stem_type,
                      ps.pool_path, ps.duration_seconds
               FROM isolation_stems s
               JOIN pool_segments ps ON ps.id = s.pool_segment_id
               WHERE s.isolation_id = ?
               ORDER BY s.stem_type""",
            (isolation_id,),
        ).fetchall()
        return [
            {
                "pool_segment_id": r["pool_segment_id"],
                "stem_type": r["stem_type"],
                "pool_path": r["pool_path"],
                "duration_seconds": r["duration_seconds"],
            }
            for r in rows
        ]

    return _retry_on_locked(_do)


# ── Track operations ───────────────────────────────────────────────

def get_tracks(project_dir: Path) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT * FROM tracks ORDER BY z_order").fetchall()
    return [{
        "id": r["id"], "name": r["name"], "z_order": r["z_order"],
        "blend_mode": r["blend_mode"], "base_opacity": r["base_opacity"],
        "muted": bool(r["muted"]) if "muted" in r.keys() else False,
        "chroma_key": json.loads(r["chroma_key"]) if r["chroma_key"] else None,
        "hidden": bool(r["hidden"]) if "hidden" in r.keys() else False,
        # Solo: when any track is solo'd, non-solo tracks are effectively
        # muted (DAW convention). Consumers compute effective_muted themselves.
        "solo": bool(r["solo"]) if "solo" in r.keys() else False,
    } for r in rows]


def add_track(project_dir: Path, track: dict):
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO tracks (id, name, z_order, blend_mode, base_opacity, muted) VALUES (?, ?, ?, ?, ?, ?)",
        (track["id"], track.get("name", "New Track"), track.get("z_order", 0),
         track.get("blend_mode", "normal"), track.get("base_opacity", 1.0),
         1 if track.get("muted", False) else 0),
    )
    conn.commit()


def update_track(project_dir: Path, track_id: str, **fields):
    conn = get_db(project_dir)
    sets = []
    values = []
    for key, val in fields.items():
        if key in ("muted", "hidden", "solo"):
            val = 1 if val else 0
        elif key == "chroma_key":
            val = json.dumps(val) if val is not None else None
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(track_id)
    conn.execute(f"UPDATE tracks SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_track(project_dir: Path, track_id: str):
    conn = get_db(project_dir)
    conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
    conn.execute("DELETE FROM opacity_keyframes WHERE track_id = ?", (track_id,))
    # Soft-delete keyframes and transitions on this track
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE keyframes SET deleted_at = ? WHERE track_id = ? AND deleted_at IS NULL", (now, track_id))
    conn.execute("UPDATE transitions SET deleted_at = ? WHERE track_id = ? AND deleted_at IS NULL", (now, track_id))
    conn.commit()


def reorder_tracks(project_dir: Path, track_ids: list[str]):
    conn = get_db(project_dir)
    for i, tid in enumerate(track_ids):
        conn.execute("UPDATE tracks SET z_order = ? WHERE id = ?", (i, tid))
    conn.commit()


# ── Opacity keyframe operations ────────────────────────────────────

def get_opacity_keyframes(project_dir: Path, track_id: str) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT * FROM opacity_keyframes WHERE track_id = ? ORDER BY time", (track_id,)).fetchall()
    return [{"id": r["id"], "track_id": r["track_id"], "time": r["time"], "opacity": r["opacity"]} for r in rows]


def add_opacity_keyframe(project_dir: Path, okf_id: str, track_id: str, time: float, opacity: float):
    conn = get_db(project_dir)
    conn.execute("INSERT OR REPLACE INTO opacity_keyframes (id, track_id, time, opacity) VALUES (?, ?, ?, ?)",
                 (okf_id, track_id, time, opacity))
    conn.commit()


def update_opacity_keyframe(project_dir: Path, okf_id: str, **fields):
    conn = get_db(project_dir)
    sets = []
    values = []
    for key, val in fields.items():
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(okf_id)
    conn.execute(f"UPDATE opacity_keyframes SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_opacity_keyframe(project_dir: Path, okf_id: str):
    conn = get_db(project_dir)
    conn.execute("DELETE FROM opacity_keyframes WHERE id = ?", (okf_id,))
    conn.commit()


# ── Marker operations ──────────────────────────────────────────────

def get_markers(project_dir: Path) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT id, time, label, type FROM markers ORDER BY time").fetchall()
    return [{"id": r["id"], "time": r["time"], "label": r["label"], "type": r["type"] or "note"} for r in rows]


def add_marker(project_dir: Path, marker_id: str, time: float, label: str = "", marker_type: str = "note"):
    conn = get_db(project_dir)
    conn.execute("INSERT OR REPLACE INTO markers (id, time, label, type) VALUES (?, ?, ?, ?)", (marker_id, time, label, marker_type))
    conn.commit()


def update_marker(project_dir: Path, marker_id: str, **fields):
    conn = get_db(project_dir)
    sets = []
    values = []
    for key, val in fields.items():
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(marker_id)
    conn.execute(f"UPDATE markers SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_marker(project_dir: Path, marker_id: str):
    conn = get_db(project_dir)
    conn.execute("DELETE FROM markers WHERE id = ?", (marker_id,))
    conn.commit()


# ── Prompt Roster ─────────────────────────────────────────────────────

def get_prompt_roster(project_dir: Path) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT id, name, template, category FROM prompt_roster ORDER BY category, name").fetchall()
    return [{"id": r["id"], "name": r["name"], "template": r["template"], "category": r["category"]} for r in rows]


def add_prompt_roster(project_dir: Path, prompt_id: str, name: str, template: str, category: str = "general"):
    conn = get_db(project_dir)
    conn.execute("INSERT OR REPLACE INTO prompt_roster (id, name, template, category) VALUES (?, ?, ?, ?)", (prompt_id, name, template, category))
    conn.commit()


def update_prompt_roster(project_dir: Path, prompt_id: str, **fields):
    conn = get_db(project_dir)
    sets = []
    values = []
    for key, val in fields.items():
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(prompt_id)
    conn.execute(f"UPDATE prompt_roster SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_prompt_roster(project_dir: Path, prompt_id: str):
    conn = get_db(project_dir)
    conn.execute("DELETE FROM prompt_roster WHERE id = ?", (prompt_id,))
    conn.commit()


# ── Timeline Validation ─────────────────────────────────────────────

def validate_timeline(project_dir: Path) -> list[str]:
    """Check timeline integrity. Returns list of warning strings (empty = healthy)."""
    warnings = []

    def parse_ts(ts):
        parts = str(ts).split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(ts) if isinstance(ts, (int, float)) else 0

    kfs = get_keyframes(project_dir)
    trs = get_transitions(project_dir)

    kf_times = {k["id"]: parse_ts(k["timestamp"]) for k in kfs}
    kf_sorted = sorted(kfs, key=lambda k: parse_ts(k["timestamp"]))

    # Build adjacency
    outgoing = {}  # kf_id -> list of tr
    incoming = {}  # kf_id -> list of tr
    for t in trs:
        outgoing.setdefault(t["from"], []).append(t)
        incoming.setdefault(t["to"], []).append(t)

    # Check each kf
    for i, kf in enumerate(kf_sorted):
        kf_id = kf["id"]
        outs = outgoing.get(kf_id, [])
        ins = incoming.get(kf_id, [])

        if len(outs) > 1:
            warnings.append(f"{kf_id}: {len(outs)} outgoing transitions")
        if len(ins) > 1:
            warnings.append(f"{kf_id}: {len(ins)} incoming transitions")
        if i > 0 and not ins:
            warnings.append(f"{kf_id}: no incoming transition")
        if i < len(kf_sorted) - 1 and not outs:
            warnings.append(f"{kf_id}: no outgoing transition")

    # Check transitions link to existing kfs and point forward in time
    active_kf_ids = set(kf_times.keys())
    for t in trs:
        if t["from"] not in active_kf_ids:
            warnings.append(f"{t['id']}: from_kf {t['from']} not found")
        elif t["to"] not in active_kf_ids:
            warnings.append(f"{t['id']}: to_kf {t['to']} not found")
        else:
            ft = kf_times[t["from"]]
            tt = kf_times[t["to"]]
            if ft > tt:
                warnings.append(f"{t['id']}: backwards {t['from']}({ft:.1f}s) -> {t['to']}({tt:.1f}s)")

    return warnings


# ── Checkpoints ────────────────────────────────────────────────────


def add_checkpoint(project_dir: Path, filename: str, name: str = "", created_at: str | None = None) -> dict:
    """Record a checkpoint's metadata. Idempotent on filename."""
    conn = get_db(project_dir)
    ts = created_at or _now_iso()
    conn.execute(
        "INSERT OR REPLACE INTO checkpoints (filename, name, created_at) VALUES (?, ?, ?)",
        (filename, name, ts),
    )
    conn.commit()
    return {"filename": filename, "name": name, "created_at": ts}


def get_checkpoint(project_dir: Path, filename: str) -> dict | None:
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT filename, name, created_at FROM checkpoints WHERE filename = ?", (filename,)
    ).fetchone()
    if not row:
        return None
    return {"filename": row[0], "name": row[1], "created_at": row[2]}


def list_checkpoints(project_dir: Path) -> list[dict]:
    """Return checkpoint metadata rows ordered newest-first."""
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT filename, name, created_at FROM checkpoints ORDER BY created_at DESC"
    ).fetchall()
    return [{"filename": r[0], "name": r[1], "created_at": r[2]} for r in rows]


def remove_checkpoint(project_dir: Path, filename: str) -> None:
    conn = get_db(project_dir)
    conn.execute("DELETE FROM checkpoints WHERE filename = ?", (filename,))
    conn.commit()


# ── Undo / Redo operations ────────────────────────────────────────


def undo_begin(project_dir: Path, description: str) -> int:
    conn = get_db(project_dir)
    conn.execute("UPDATE undo_state SET value = value + 1 WHERE key = 'current_group'")
    row = conn.execute("SELECT value FROM undo_state WHERE key = 'current_group'").fetchone()
    if row is None:
        conn.execute("INSERT INTO undo_state (key, value) VALUES ('current_group', 1)")
        group_id = 1
    else:
        group_id = row[0]
    # If this group_id already exists (stale counter), bump past it
    existing = conn.execute("SELECT MAX(id) FROM undo_groups").fetchone()
    if existing and existing[0] is not None and group_id <= existing[0]:
        group_id = existing[0] + 1
        conn.execute("UPDATE undo_state SET value = ? WHERE key = 'current_group'", (group_id,))
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO undo_groups (id, description, timestamp) VALUES (?, ?, ?)",
        (group_id, description, datetime.now(timezone.utc).isoformat())
    )
    # Clear redo history (new operation invalidates redo stack)
    conn.execute("DELETE FROM redo_log WHERE undo_group IN (SELECT id FROM undo_groups WHERE undone = 1)")
    conn.execute("DELETE FROM undo_log WHERE undo_group IN (SELECT id FROM undo_groups WHERE undone = 1)")
    conn.execute("DELETE FROM undo_groups WHERE undone = 1")
    # Prune old history (keep max 1000 groups)
    conn.execute("""
        DELETE FROM undo_log WHERE undo_group IN (
            SELECT id FROM undo_groups WHERE id NOT IN (
                SELECT id FROM undo_groups ORDER BY id DESC LIMIT 1000
            )
        )
    """)
    conn.execute("DELETE FROM undo_groups WHERE id NOT IN (SELECT id FROM undo_groups ORDER BY id DESC LIMIT 1000)")
    conn.commit()
    return group_id


def undo_execute(project_dir: Path) -> dict | None:
    conn = get_db(project_dir)
    group = conn.execute(
        "SELECT id, description, timestamp FROM undo_groups WHERE undone = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not group:
        return None
    group_id = group["id"]

    # Use a temporary redo_group to capture redo data via triggers.
    # Keep triggers ENABLED while running inverse SQL — triggers capture
    # the inverse-of-inverse (= original forward SQL) into undo_log.
    # Then move those entries to redo_log for later redo.
    redo_capture_group = -group_id  # negative ID to avoid collision
    conn.execute("UPDATE undo_state SET value = ? WHERE key = 'current_group'", (redo_capture_group,))

    rows = conn.execute(
        "SELECT sql_text FROM undo_log WHERE undo_group = ? ORDER BY seq DESC",
        (group_id,)
    ).fetchall()
    for row in rows:
        conn.execute(row["sql_text"])

    # Move captured entries from undo_log to redo_log
    conn.execute(
        "INSERT INTO redo_log (undo_group, sql_text) SELECT ?, sql_text FROM undo_log WHERE undo_group = ? ORDER BY seq",
        (group_id, redo_capture_group),
    )
    conn.execute("DELETE FROM undo_log WHERE undo_group = ?", (redo_capture_group,))

    # Restore current_group and mark as undone
    conn.execute("UPDATE undo_state SET value = ? WHERE key = 'current_group'", (group_id,))
    conn.execute("UPDATE undo_groups SET undone = 1 WHERE id = ?", (group_id,))
    conn.commit()
    return {"id": group_id, "description": group["description"], "timestamp": group["timestamp"]}


def redo_execute(project_dir: Path) -> dict | None:
    """Redo the last undone operation.

    Redo works by re-executing the redo_log entries captured during undo.
    When undo_execute runs, triggers are temporarily re-enabled while executing
    the inverse SQL, which captures the "forward" SQL into a redo group.
    """
    conn = get_db(project_dir)
    group = conn.execute(
        "SELECT id, description, timestamp FROM undo_groups WHERE undone = 1 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not group:
        return None
    group_id = group["id"]

    # Check if we have redo data (captured during undo)
    redo_rows = conn.execute(
        "SELECT sql_text FROM redo_log WHERE undo_group = ? ORDER BY seq ASC",
        (group_id,)
    ).fetchall()
    if not redo_rows:
        return None

    # Disable triggers during redo replay
    conn.execute("UPDATE undo_state SET value = 0 WHERE key = 'active'")
    for row in redo_rows:
        conn.execute(row["sql_text"])
    conn.execute("UPDATE undo_state SET value = 1 WHERE key = 'active'")

    # Mark as no longer undone
    conn.execute("UPDATE undo_groups SET undone = 0 WHERE id = ?", (group_id,))
    # Clean up redo data
    conn.execute("DELETE FROM redo_log WHERE undo_group = ?", (group_id,))
    conn.commit()

    return {"id": group_id, "description": group["description"], "timestamp": group["timestamp"]}


def undo_history(project_dir: Path, limit: int = 50) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT id, description, timestamp, undone FROM undo_groups ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [{"id": r["id"], "description": r["description"], "timestamp": r["timestamp"], "undone": bool(r["undone"])} for r in rows]


# ── Sections ──

def _row_to_section(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "label": row["label"],
        "start": row["start"],
        "end": row["end"],
        "mood": row["mood"],
        "energy": row["energy"],
        "instruments": json.loads(row["instruments"]),
        "motifs": json.loads(row["motifs"]),
        "events": json.loads(row["events"]),
        "visual_direction": row["visual_direction"],
        "notes": row["notes"],
    }


def get_sections(project_dir: Path) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute('SELECT * FROM sections ORDER BY sort_order').fetchall()
    return [_row_to_section(r) for r in rows]


def set_sections(project_dir: Path, sections: list[dict]):
    conn = get_db(project_dir)
    conn.execute("DELETE FROM sections")
    for i, sec in enumerate(sections):
        conn.execute(
            """INSERT INTO sections (id, label, start, "end", mood, energy, instruments, motifs, events, visual_direction, notes, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sec.get("id", f"section_{i}"), sec.get("label", ""),
             sec.get("start", "0:00"), sec.get("end"),
             sec.get("mood", ""), sec.get("energy", ""),
             json.dumps(sec.get("instruments", [])),
             json.dumps(sec.get("motifs", [])),
             json.dumps(sec.get("events", [])),
             sec.get("visual_direction", ""),
             sec.get("notes", ""), i),
        )
    conn.commit()


# ── Audio track operations ────────────────────────────────────────

_DEFAULT_VOLUME_CURVE = '[[0,0],[1,0]]'


def get_audio_tracks(project_dir: Path) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute("SELECT * FROM audio_tracks ORDER BY display_order").fetchall()
    return [{
        "id": r["id"], "name": r["name"], "display_order": r["display_order"],
        "hidden": bool(r["hidden"]),
        "muted": bool(r["muted"]),
        # `solo` column may not exist on un-migrated rows — sqlite3.Row raises
        # IndexError for unknown keys; guard with keys() lookup.
        "solo": bool(r["solo"]) if "solo" in r.keys() else False,
        "volume_curve": json.loads(r["volume_curve"]) if r["volume_curve"] else [[0, 0], [1, 0]],
    } for r in rows]


def add_audio_track(project_dir: Path, track: dict):
    conn = get_db(project_dir)
    vc = track.get("volume_curve")
    if vc is None:
        vc_str = _DEFAULT_VOLUME_CURVE
    elif isinstance(vc, str):
        vc_str = vc
    else:
        vc_str = json.dumps(vc)
    conn.execute(
        "INSERT INTO audio_tracks (id, name, display_order, hidden, muted, solo, volume_curve) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (track["id"], track.get("name", "Audio Track"),
         track.get("display_order", 0),
         1 if track.get("hidden", False) else 0,
         1 if track.get("muted", False) else 0,
         1 if track.get("solo", False) else 0,
         vc_str),
    )
    conn.commit()


def update_audio_track(project_dir: Path, track_id: str, **fields):
    conn = get_db(project_dir)
    sets = []
    values = []
    for key, val in fields.items():
        if key in ("hidden", "muted", "solo"):
            val = 1 if val else 0
        elif key == "volume_curve" and not isinstance(val, str):
            val = json.dumps(val)
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(track_id)
    conn.execute(f"UPDATE audio_tracks SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_audio_track(project_dir: Path, track_id: str):
    conn = get_db(project_dir)
    conn.execute("DELETE FROM audio_tracks WHERE id = ?", (track_id,))
    # Soft-delete audio clips on this track
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE audio_clips SET deleted_at = ? WHERE track_id = ? AND deleted_at IS NULL", (now, track_id))
    conn.commit()


def reorder_audio_tracks(project_dir: Path, track_ids: list[str]):
    conn = get_db(project_dir)
    for i, tid in enumerate(track_ids):
        conn.execute("UPDATE audio_tracks SET display_order = ? WHERE id = ?", (i, tid))
    conn.commit()


# ── Audio clip operations ─────────────────────────────────────────

def get_audio_clips(project_dir: Path, track_id: str | None = None) -> list[dict]:
    """Return audio clips enriched with derived playback fields.

    For clips linked to a transition (`audio_clip_links`), `playback_rate` and
    `effective_source_offset` reflect the transition's linear remap:

        rate     = source_span / kf_span
        eff_off  = trim_in + stored_source_offset

    where `source_span = (trim_out or source_duration) - trim_in` and
    `kf_span = to_kf.ts - from_kf.ts`. For unlinked clips, rate=1.0 and
    eff_off == stored source_offset. The stored `source_offset` column is
    untouched — callers that want the raw value can still read it.
    """
    conn = get_db(project_dir)
    if track_id:
        rows = conn.execute(
            "SELECT * FROM audio_clips WHERE track_id = ? AND deleted_at IS NULL ORDER BY start_time",
            (track_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audio_clips WHERE deleted_at IS NULL ORDER BY track_id, start_time").fetchall()

    # Preload all links + their transitions in one pass to avoid N+1 queries
    clip_ids = [r["id"] for r in rows]
    link_map: dict[str, str] = {}  # audio_clip_id → transition_id
    if clip_ids:
        placeholders = ",".join("?" for _ in clip_ids)
        link_rows = conn.execute(
            f"SELECT audio_clip_id, transition_id FROM audio_clip_links WHERE audio_clip_id IN ({placeholders})",
            clip_ids,
        ).fetchall()
        for lr in link_rows:
            link_map[lr["audio_clip_id"]] = lr["transition_id"]

    # Fetch linked transitions + their keyframes in bulk
    tr_cache: dict[str, dict] = {}
    kf_cache: dict[str, dict] = {}
    if link_map:
        tr_ids = list(set(link_map.values()))
        tr_placeholders = ",".join("?" for _ in tr_ids)
        tr_rows = conn.execute(
            f"SELECT id, from_kf, to_kf, trim_in, trim_out, source_video_duration FROM transitions WHERE id IN ({tr_placeholders})",
            tr_ids,
        ).fetchall()
        kf_ids = set()
        for tr in tr_rows:
            tr_cache[tr["id"]] = {
                "from_kf": tr["from_kf"], "to_kf": tr["to_kf"],
                "trim_in": tr["trim_in"] if tr["trim_in"] is not None else 0.0,
                "trim_out": tr["trim_out"],
                "source_video_duration": tr["source_video_duration"],
            }
            if tr["from_kf"]: kf_ids.add(tr["from_kf"])
            if tr["to_kf"]: kf_ids.add(tr["to_kf"])
        if kf_ids:
            kf_placeholders = ",".join("?" for _ in kf_ids)
            kf_rows = conn.execute(
                f"SELECT id, timestamp FROM keyframes WHERE id IN ({kf_placeholders})",
                list(kf_ids),
            ).fetchall()
            for kf in kf_rows:
                ts_str = kf["timestamp"]
                ts_val: float = 0.0
                try:
                    parts = str(ts_str).split(":")
                    if len(parts) == 1: ts_val = float(parts[0])
                    elif len(parts) == 2: ts_val = float(parts[0]) * 60 + float(parts[1])
                    elif len(parts) == 3: ts_val = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                except (ValueError, TypeError):
                    ts_val = 0.0
                kf_cache[kf["id"]] = {"timestamp": ts_val}

    def _derive(clip_id: str, stored_offset: float, start_time: float, end_time: float) -> tuple[float, float]:
        """Return (playback_rate, effective_source_offset) for this clip."""
        tr_id = link_map.get(clip_id)
        if not tr_id:
            return 1.0, stored_offset
        tr = tr_cache.get(tr_id)
        if not tr:
            return 1.0, stored_offset
        from_kf = kf_cache.get(tr["from_kf"])
        to_kf = kf_cache.get(tr["to_kf"])
        if not from_kf or not to_kf:
            return 1.0, stored_offset
        kf_span = to_kf["timestamp"] - from_kf["timestamp"]
        trim_in = float(tr["trim_in"])
        trim_out = tr["trim_out"]
        if trim_out is None:
            trim_out = tr["source_video_duration"]
        if trim_out is None:
            # Fall back to kf_span — neither trim_out nor source duration known
            return 1.0, stored_offset + trim_in
        source_span = float(trim_out) - trim_in
        if kf_span <= 0 or source_span <= 0:
            return 1.0, stored_offset + trim_in
        rate = source_span / kf_span
        return rate, stored_offset + trim_in

    # Bulk-resolve variant_kind from pool_segments via audio_clips.selected FK
    vk_map: dict[str, str | None] = {}
    selected_ids = [r["selected"] for r in rows if r["selected"]]
    if selected_ids:
        vk_placeholders = ",".join("?" for _ in selected_ids)
        vk_rows = conn.execute(
            f"SELECT id, variant_kind FROM pool_segments WHERE id IN ({vk_placeholders})",
            selected_ids,
        ).fetchall()
        for vr in vk_rows:
            vk_map[vr["id"]] = vr["variant_kind"]

    result = []
    for r in rows:
        rate, eff_off = _derive(r["id"], float(r["source_offset"]), float(r["start_time"]), float(r["end_time"]))
        result.append({
            "id": r["id"], "track_id": r["track_id"],
            "source_path": r["source_path"],
            "start_time": r["start_time"], "end_time": r["end_time"],
            "source_offset": r["source_offset"],
            "volume_curve": json.loads(r["volume_curve"]) if r["volume_curve"] else [[0, 0], [1, 0]],
            "muted": bool(r["muted"]),
            "remap": json.loads(r["remap"]) if r["remap"] else {"method": "linear", "target_duration": 0},
            "selected": r["selected"],
            "label": r["label"],
            "variant_kind": vk_map.get(r["selected"]) if r["selected"] else None,
            # Derived fields (computed from linked transition at query time; not stored)
            "playback_rate": rate,
            "effective_source_offset": eff_off,
            # M10 cross-type drag uses this so a linked clip isn't manually
            # shifted when its transition is also moved — propagation via
            # update_keyframe handles the linked-audio shift automatically.
            "linked_transition_id": link_map.get(r["id"]),
        })
    return result


def add_audio_clip(project_dir: Path, clip: dict):
    conn = get_db(project_dir)
    vc = clip.get("volume_curve")
    if vc is None:
        vc_str = _DEFAULT_VOLUME_CURVE
    elif isinstance(vc, str):
        vc_str = vc
    else:
        vc_str = json.dumps(vc)
    conn.execute(
        """INSERT INTO audio_clips (id, track_id, source_path, start_time, end_time, source_offset, volume_curve, muted, remap, label)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (clip["id"], clip["track_id"], clip.get("source_path", ""),
         clip.get("start_time", 0), clip.get("end_time", 0),
         clip.get("source_offset", 0), vc_str,
         1 if clip.get("muted", False) else 0,
         json.dumps(clip.get("remap", {"method": "linear", "target_duration": 0})),
         clip.get("label")),
    )
    conn.commit()


def update_audio_clip(project_dir: Path, clip_id: str, **fields):
    conn = get_db(project_dir)
    sets = []
    values = []
    for key, val in fields.items():
        if key == "muted":
            val = 1 if val else 0
        elif key == "remap":
            val = json.dumps(val) if isinstance(val, dict) else val
        elif key == "volume_curve" and not isinstance(val, str):
            val = json.dumps(val)
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(clip_id)
    conn.execute(f"UPDATE audio_clips SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_audio_clip(project_dir: Path, clip_id: str):
    conn = get_db(project_dir)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE audio_clips SET deleted_at = ? WHERE id = ?", (now, clip_id))
    conn.commit()


# ── Audio clip link operations (clips ↔ transitions) ───────────────

def add_audio_clip_link(project_dir: Path, audio_clip_id: str, transition_id: str, offset: float = 0.0):
    """Link an audio clip to a transition. `offset` is user-intent anchor in seconds."""
    conn = get_db(project_dir)
    conn.execute(
        """INSERT INTO audio_clip_links (audio_clip_id, transition_id, offset) VALUES (?, ?, ?)
           ON CONFLICT(audio_clip_id, transition_id) DO UPDATE SET offset = excluded.offset""",
        (audio_clip_id, transition_id, offset),
    )
    conn.commit()


def get_audio_clip_links_for_transition(project_dir: Path, transition_id: str) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT audio_clip_id, transition_id, offset FROM audio_clip_links WHERE transition_id = ?",
        (transition_id,),
    ).fetchall()
    return [{"audio_clip_id": r["audio_clip_id"], "transition_id": r["transition_id"], "offset": r["offset"]} for r in rows]


def get_audio_clip_links_for_clip(project_dir: Path, audio_clip_id: str) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT audio_clip_id, transition_id, offset FROM audio_clip_links WHERE audio_clip_id = ?",
        (audio_clip_id,),
    ).fetchall()
    return [{"audio_clip_id": r["audio_clip_id"], "transition_id": r["transition_id"], "offset": r["offset"]} for r in rows]


def remove_audio_clip_link(project_dir: Path, audio_clip_id: str, transition_id: str):
    conn = get_db(project_dir)
    conn.execute(
        "DELETE FROM audio_clip_links WHERE audio_clip_id = ? AND transition_id = ?",
        (audio_clip_id, transition_id),
    )
    conn.commit()


def remove_audio_clip_links_for_transition(project_dir: Path, transition_id: str) -> list[str]:
    """Remove all links for a transition. Returns the list of audio_clip_ids that were unlinked."""
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT audio_clip_id FROM audio_clip_links WHERE transition_id = ?",
        (transition_id,),
    ).fetchall()
    clip_ids = [r["audio_clip_id"] for r in rows]
    conn.execute("DELETE FROM audio_clip_links WHERE transition_id = ?", (transition_id,))
    conn.commit()
    return clip_ids


def update_audio_clip_link_offset(project_dir: Path, audio_clip_id: str, transition_id: str, offset: float):
    conn = get_db(project_dir)
    conn.execute(
        "UPDATE audio_clip_links SET offset = ? WHERE audio_clip_id = ? AND transition_id = ?",
        (offset, audio_clip_id, transition_id),
    )
    conn.commit()


# ── M16 music generation helpers ──────────────────────────────────────────

def add_music_generation(
    project_dir: Path,
    *,
    generation_id: str,
    action: str,
    model: str,
    instrumental: int,
    style: str | None = None,
    lyrics: str | None = None,
    title: str | None = None,
    gender: str | None = None,
    singer_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    reused_from: str | None = None,
    created_by: str = "plugin:generate-music",
    status: str = "pending",
    task_ids: list[str] | None = None,
) -> str:
    """Insert a new music_generations row. Returns the id."""
    from datetime import datetime, timezone
    conn = get_db(project_dir)
    conn.execute(
        """INSERT INTO generate_music__generations (
            id, action, model, style, lyrics, title, instrumental, gender,
            singer_id, task_ids_json, status, entity_type, entity_id,
            reused_from, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            generation_id, action, model, style, lyrics, title, instrumental,
            gender, singer_id, json.dumps(task_ids or []), status, entity_type,
            entity_id, reused_from, created_by,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return generation_id


def update_music_generation_status(
    project_dir: Path,
    generation_id: str,
    status: str,
    *,
    error: str | None = None,
    task_ids: list[str] | None = None,
) -> None:
    conn = get_db(project_dir)
    if task_ids is not None:
        conn.execute(
            "UPDATE generate_music__generations SET status = ?, error = ?, task_ids_json = ? WHERE id = ?",
            (status, error, json.dumps(task_ids), generation_id),
        )
    else:
        conn.execute(
            "UPDATE generate_music__generations SET status = ?, error = ? WHERE id = ?",
            (status, error, generation_id),
        )
    conn.commit()


def add_generation_track(
    project_dir: Path,
    *,
    generation_id: str,
    pool_segment_id: str,
    musicful_task_id: str,
    song_title: str | None = None,
    duration_seconds: float | None = None,
    cover_url: str | None = None,
    created_by: str = "plugin:generate-music",
) -> None:
    conn = get_db(project_dir)
    conn.execute(
        """INSERT INTO generate_music__tracks (
            generation_id, pool_segment_id, musicful_task_id, song_title,
            duration_seconds, cover_url, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (generation_id, pool_segment_id, musicful_task_id, song_title,
         duration_seconds, cover_url, created_by),
    )
    conn.commit()


def get_music_generation(project_dir: Path, generation_id: str) -> dict | None:
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT * FROM generate_music__generations WHERE id = ?",
        (generation_id,),
    ).fetchone()
    return dict(row) if row else None


def get_music_generations_for_entity(
    project_dir: Path,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """List music generations with their tracks joined. Filters by entity
    when both entity_type and entity_id are provided; otherwise returns all."""
    conn = get_db(project_dir)
    if entity_type and entity_id:
        gen_rows = conn.execute(
            "SELECT * FROM generate_music__generations WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC LIMIT ?",
            (entity_type, entity_id, limit),
        ).fetchall()
    else:
        gen_rows = conn.execute(
            "SELECT * FROM generate_music__generations ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    generations = []
    for g in gen_rows:
        tracks = conn.execute(
            """SELECT t.*, p.pool_path, p.duration_seconds AS pool_duration
               FROM generate_music__tracks t
               JOIN pool_segments p ON p.id = t.pool_segment_id
               WHERE t.generation_id = ?""",
            (g["id"],),
        ).fetchall()
        gd = dict(g)
        gd["task_ids"] = json.loads(g["task_ids_json"] or "[]")
        gd["tracks"] = [dict(t) for t in tracks]
        generations.append(gd)
    return generations


def get_music_generation_tracks(project_dir: Path, generation_id: str) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM generate_music__tracks WHERE generation_id = ?",
        (generation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── M18 foley generation helpers ─────────────────────────────────────────
# Mirrors the M16 music helpers. Same shape; foley-specific columns.


def add_foley_generation(
    project_dir: Path,
    *,
    generation_id: str,
    mode: str,
    model: str,
    prompt: str | None = None,
    duration_seconds: float | None = None,
    source_candidate_id: str | None = None,
    source_in_seconds: float | None = None,
    source_out_seconds: float | None = None,
    negative_prompt: str | None = None,
    cfg_strength: float | None = None,
    seed: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    variant_count: int = 1,
    status: str = "pending",
    created_by: str = "plugin:generate-foley",
) -> str:
    """Insert a new generate_foley__generations row. Returns the id."""
    from datetime import datetime, timezone
    conn = get_db(project_dir)
    conn.execute(
        """INSERT INTO generate_foley__generations (
            id, created_at, created_by, mode, prompt, duration_seconds,
            source_candidate_id, source_in_seconds, source_out_seconds,
            model, negative_prompt, cfg_strength, seed,
            entity_type, entity_id, variant_count, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            generation_id,
            datetime.now(timezone.utc).isoformat(),
            created_by,
            mode, prompt, duration_seconds,
            source_candidate_id, source_in_seconds, source_out_seconds,
            model, negative_prompt, cfg_strength, seed,
            entity_type, entity_id, variant_count, status,
        ),
    )
    conn.commit()
    return generation_id


def update_foley_generation_status(
    project_dir: Path,
    generation_id: str,
    status: str,
    *,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> None:
    """Update status + optional error/timestamps on a foley generation row."""
    conn = get_db(project_dir)
    # Build the SET list dynamically to only touch provided fields
    fields = ["status = ?", "error = ?"]
    params: list = [status, error]
    if started_at is not None:
        fields.append("started_at = ?")
        params.append(started_at)
    if completed_at is not None:
        fields.append("completed_at = ?")
        params.append(completed_at)
    params.append(generation_id)
    conn.execute(
        f"UPDATE generate_foley__generations SET {', '.join(fields)} WHERE id = ?",
        tuple(params),
    )
    conn.commit()


def add_foley_track(
    project_dir: Path,
    *,
    generation_id: str,
    pool_segment_id: str,
    variant_index: int,
    replicate_prediction_id: str,
    duration_seconds: float | None = None,
    spend_ledger_id: str | None = None,
    created_by: str = "plugin:generate-foley",
) -> None:
    """Insert a generate_foley__tracks junction row."""
    conn = get_db(project_dir)
    conn.execute(
        """INSERT INTO generate_foley__tracks (
            generation_id, pool_segment_id, variant_index,
            replicate_prediction_id, duration_seconds, spend_ledger_id,
            created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (generation_id, pool_segment_id, variant_index,
         replicate_prediction_id, duration_seconds, spend_ledger_id,
         created_by),
    )
    conn.commit()


def get_foley_generation(project_dir: Path, generation_id: str) -> dict | None:
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT * FROM generate_foley__generations WHERE id = ?",
        (generation_id,),
    ).fetchone()
    return dict(row) if row else None


def get_foley_generations_for_entity(
    project_dir: Path,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """List foley generations with their tracks joined. Filters by entity
    when both entity_type and entity_id are provided; otherwise returns all."""
    conn = get_db(project_dir)
    if entity_type and entity_id:
        gen_rows = conn.execute(
            "SELECT * FROM generate_foley__generations WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC LIMIT ?",
            (entity_type, entity_id, limit),
        ).fetchall()
    else:
        gen_rows = conn.execute(
            "SELECT * FROM generate_foley__generations ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    generations = []
    for g in gen_rows:
        tracks = conn.execute(
            """SELECT t.*, p.pool_path, p.duration_seconds AS pool_duration
               FROM generate_foley__tracks t
               JOIN pool_segments p ON p.id = t.pool_segment_id
               WHERE t.generation_id = ?
               ORDER BY t.variant_index ASC""",
            (g["id"],),
        ).fetchall()
        gd = dict(g)
        gd["tracks"] = [dict(t) for t in tracks]
        generations.append(gd)
    return generations


def get_foley_generation_tracks(project_dir: Path, generation_id: str) -> list[dict]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM generate_foley__tracks WHERE generation_id = ? ORDER BY variant_index ASC",
        (generation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── M16 pool_segments context helpers ────────────────────────────────────

def set_pool_segment_context(
    project_dir: Path,
    pool_segment_id: str,
    *,
    context_entity_type: str | None,
    context_entity_id: str | None,
    variant_kind: str | None = None,
) -> None:
    """Stamp a pool_segment with M16's weak-context-provenance fields.
    Per spec R10/R40: variant_kind='music' drives UI coloring; context_entity_*
    records where/why the segment was generated. Independent of derived_from
    (which stays M13's typed pool_segment FK)."""
    conn = get_db(project_dir)
    if variant_kind is not None:
        conn.execute(
            "UPDATE pool_segments SET context_entity_type = ?, context_entity_id = ?, variant_kind = ? WHERE id = ?",
            (context_entity_type, context_entity_id, variant_kind, pool_segment_id),
        )
    else:
        conn.execute(
            "UPDATE pool_segments SET context_entity_type = ?, context_entity_id = ? WHERE id = ?",
            (context_entity_type, context_entity_id, pool_segment_id),
        )
    conn.commit()


# ── M13 task-45: track_effects + effect_curves + buses + labels ──────────
# Spec: local.effect-curves-macro-panel.md, R1-R5. These are CORE helpers
# (no plugin prefix); the frontend mixer + HTTP endpoints in api_server.py
# call them directly. Return shapes use `db_models.py` dataclasses — see
# that module for the TS-parity field names.

from scenecraft.db_models import (
    TrackEffect as _TrackEffect,
    EffectCurve as _EffectCurve,
    SendBus as _SendBus,
    TrackSend as _TrackSend,
    FrequencyLabel as _FrequencyLabel,
)


def _row_to_track_effect(row: sqlite3.Row) -> _TrackEffect:
    sp = row["static_params"]
    return _TrackEffect(
        id=row["id"],
        track_id=row["track_id"],
        effect_type=row["effect_type"],
        order_index=row["order_index"],
        enabled=bool(row["enabled"]),
        static_params=json.loads(sp) if sp else {},
        created_at=row["created_at"],
    )


def _row_to_effect_curve(row: sqlite3.Row) -> _EffectCurve:
    pts = row["points"]
    return _EffectCurve(
        id=row["id"],
        effect_id=row["effect_id"],
        param_name=row["param_name"],
        points=json.loads(pts) if pts else [],
        interpolation=row["interpolation"],
        visible=bool(row["visible"]),
    )


def _row_to_send_bus(row: sqlite3.Row) -> _SendBus:
    sp = row["static_params"]
    return _SendBus(
        id=row["id"],
        bus_type=row["bus_type"],
        label=row["label"],
        order_index=row["order_index"],
        static_params=json.loads(sp) if sp else {},
    )


def _row_to_track_send(row: sqlite3.Row) -> _TrackSend:
    return _TrackSend(
        track_id=row["track_id"],
        bus_id=row["bus_id"],
        level=float(row["level"]),
    )


def _row_to_frequency_label(row: sqlite3.Row) -> _FrequencyLabel:
    return _FrequencyLabel(
        id=row["id"],
        label=row["label"],
        freq_min_hz=float(row["freq_min_hz"]),
        freq_max_hz=float(row["freq_max_hz"]),
    )


# ── track_effects ─────────────────────────────────────────────────────────

def add_track_effect(
    project_dir: Path,
    *,
    track_id: str,
    effect_type: str,
    static_params: dict | None = None,
    order_index: int | None = None,
    enabled: bool = True,
) -> _TrackEffect:
    """Insert a new track_effects row. Generates an id, defaults
    ``order_index`` to max(existing)+1 when not provided, and returns the
    hydrated TrackEffect dataclass. Does NOT validate ``effect_type`` against
    the registry — that's the HTTP endpoint's job (spec R_V1)."""
    conn = get_db(project_dir)
    eff_id = generate_id("eff")
    sp_json = json.dumps(static_params or {})
    if order_index is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM track_effects WHERE track_id = ?",
            (track_id,),
        ).fetchone()
        order_index = int(row[0])
    conn.execute(
        "INSERT INTO track_effects (id, track_id, effect_type, order_index, enabled, static_params, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (eff_id, track_id, effect_type, order_index, 1 if enabled else 0, sp_json, _now_iso()),
    )
    conn.commit()
    return get_track_effect(project_dir, eff_id)  # type: ignore[return-value]


def get_track_effect(project_dir: Path, effect_id: str) -> _TrackEffect | None:
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT * FROM track_effects WHERE id = ?", (effect_id,)
    ).fetchone()
    return _row_to_track_effect(row) if row else None


def list_track_effects(project_dir: Path, track_id: str) -> list[_TrackEffect]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM track_effects WHERE track_id = ? ORDER BY order_index",
        (track_id,),
    ).fetchall()
    return [_row_to_track_effect(r) for r in rows]


def update_track_effect(project_dir: Path, effect_id: str, **fields) -> None:
    """Partial UPDATE. Accepts any of: order_index, enabled, static_params."""
    if not fields:
        return
    conn = get_db(project_dir)
    sets: list[str] = []
    values: list[Any] = []
    for key, val in fields.items():
        if key == "enabled":
            val = 1 if val else 0
        elif key == "static_params" and not isinstance(val, str):
            val = json.dumps(val)
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(effect_id)
    conn.execute(f"UPDATE track_effects SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_track_effect(project_dir: Path, effect_id: str) -> None:
    """DELETE a track_effect. ON DELETE CASCADE removes associated effect_curves.
    Idempotent: deleting a non-existent id is a no-op (spec R_V1)."""
    conn = get_db(project_dir)
    conn.execute("DELETE FROM track_effects WHERE id = ?", (effect_id,))
    conn.commit()


# ── master-bus effects ────────────────────────────────────────────────────
# Master-bus effects are ``track_effects`` rows with ``track_id IS NULL``. They
# process the summed mix (output of ``masterGain`` before ``destination``) and
# are the canonical home for a master limiter / bus compressor. Track-effect
# helpers (``add_track_effect``, ``list_track_effects``, ``get_track_effect``)
# scope by ``track_id = ?`` which already excludes NULL rows — no changes
# needed there. ``effect_curves.effect_id`` is a FK to ``track_effects.id``
# regardless of NULL-ness, so automation on master-bus effects works without
# any additional table changes.

def add_master_bus_effect(
    project_dir: Path,
    *,
    effect_type: str,
    static_params: dict | None = None,
    order_index: int | None = None,
    enabled: bool = True,
) -> _TrackEffect:
    """Insert a master-bus ``track_effects`` row (``track_id IS NULL``).

    Defaults ``order_index`` to max(existing master-bus order_index)+1 when
    not provided. Mirrors ``add_track_effect`` in every other respect; does
    NOT validate ``effect_type`` against the registry (caller's job).
    """
    conn = get_db(project_dir)
    eff_id = generate_id("eff")
    sp_json = json.dumps(static_params or {})
    if order_index is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM track_effects WHERE track_id IS NULL"
        ).fetchone()
        order_index = int(row[0])
    conn.execute(
        "INSERT INTO track_effects (id, track_id, effect_type, order_index, enabled, static_params, created_at) "
        "VALUES (?, NULL, ?, ?, ?, ?, ?)",
        (eff_id, effect_type, order_index, 1 if enabled else 0, sp_json, _now_iso()),
    )
    conn.commit()
    return get_master_bus_effect(project_dir, eff_id)  # type: ignore[return-value]


def list_master_bus_effects(project_dir: Path) -> list[_TrackEffect]:
    """Return all master-bus effects (``track_id IS NULL``) ordered by
    ``order_index`` ascending. Does not return any track-scoped rows."""
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM track_effects WHERE track_id IS NULL ORDER BY order_index"
    ).fetchall()
    return [_row_to_track_effect(r) for r in rows]


def get_master_bus_effect(project_dir: Path, effect_id: str) -> _TrackEffect | None:
    """Fetch a master-bus effect by id. Returns None if the id does not exist
    OR if it belongs to a track (``track_id IS NOT NULL``). This scoping lets
    chat tools distinguish "track-effect id accidentally passed to a
    master-bus API" from "missing effect" without a secondary lookup.
    """
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT * FROM track_effects WHERE id = ? AND track_id IS NULL",
        (effect_id,),
    ).fetchone()
    return _row_to_track_effect(row) if row else None


# ── effect_curves ─────────────────────────────────────────────────────────

def add_effect_curve(
    project_dir: Path,
    *,
    effect_id: str,
    param_name: str,
    points: list | None = None,
    interpolation: str = "bezier",
    visible: bool = False,
) -> _EffectCurve:
    """Straight INSERT — raises sqlite3.IntegrityError on duplicate
    (effect_id, param_name). Use ``upsert_effect_curve`` for UPSERT semantics."""
    conn = get_db(project_dir)
    curve_id = generate_id("curve")
    pts_json = json.dumps(points or [])
    conn.execute(
        "INSERT INTO effect_curves (id, effect_id, param_name, points, interpolation, visible) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (curve_id, effect_id, param_name, pts_json, interpolation, 1 if visible else 0),
    )
    conn.commit()
    return get_effect_curve(project_dir, curve_id)  # type: ignore[return-value]


def get_effect_curve(project_dir: Path, curve_id: str) -> _EffectCurve | None:
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT * FROM effect_curves WHERE id = ?", (curve_id,)
    ).fetchone()
    return _row_to_effect_curve(row) if row else None


def list_curves_for_effect(project_dir: Path, effect_id: str) -> list[_EffectCurve]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM effect_curves WHERE effect_id = ? ORDER BY param_name",
        (effect_id,),
    ).fetchall()
    return [_row_to_effect_curve(r) for r in rows]


def upsert_effect_curve(
    project_dir: Path,
    *,
    effect_id: str,
    param_name: str,
    points: list | None = None,
    interpolation: str = "bezier",
    visible: bool = False,
) -> _EffectCurve:
    """Insert or update the single row for (effect_id, param_name).

    Spec R2: the UNIQUE(effect_id, param_name) constraint means the
    application path for updating an existing curve must UPSERT rather than
    re-INSERT. A raw duplicate INSERT attempt still fails at the SQL layer —
    that's the required failure mode for the `effect-curves-unique-constraint`
    test. This helper uses ``ON CONFLICT ... DO UPDATE`` so the row's ``id``
    stays stable across updates (important for clients that cache by id).
    """
    conn = get_db(project_dir)
    pts_json = json.dumps(points or [])
    curve_id = generate_id("curve")
    conn.execute(
        "INSERT INTO effect_curves (id, effect_id, param_name, points, interpolation, visible) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(effect_id, param_name) DO UPDATE SET "
        "points = excluded.points, interpolation = excluded.interpolation, "
        "visible = excluded.visible",
        (curve_id, effect_id, param_name, pts_json, interpolation, 1 if visible else 0),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM effect_curves WHERE effect_id = ? AND param_name = ?",
        (effect_id, param_name),
    ).fetchone()
    assert row is not None, "upsert should always yield a row"
    return _row_to_effect_curve(row)


def update_effect_curve(project_dir: Path, curve_id: str, **fields) -> None:
    if not fields:
        return
    conn = get_db(project_dir)
    sets: list[str] = []
    values: list[Any] = []
    for key, val in fields.items():
        if key == "visible":
            val = 1 if val else 0
        elif key == "points" and not isinstance(val, str):
            val = json.dumps(val)
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(curve_id)
    conn.execute(f"UPDATE effect_curves SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_effect_curve(project_dir: Path, curve_id: str) -> None:
    conn = get_db(project_dir)
    conn.execute("DELETE FROM effect_curves WHERE id = ?", (curve_id,))
    conn.commit()


# ── project_send_buses ────────────────────────────────────────────────────

def add_send_bus(
    project_dir: Path,
    *,
    bus_type: str,
    label: str,
    static_params: dict | None = None,
    order_index: int | None = None,
) -> _SendBus:
    conn = get_db(project_dir)
    bus_id = generate_id("bus")
    sp_json = json.dumps(static_params or {})
    if order_index is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM project_send_buses"
        ).fetchone()
        order_index = int(row[0])
    conn.execute(
        "INSERT INTO project_send_buses (id, bus_type, label, order_index, static_params) "
        "VALUES (?, ?, ?, ?, ?)",
        (bus_id, bus_type, label, order_index, sp_json),
    )
    conn.commit()
    return get_send_bus(project_dir, bus_id)  # type: ignore[return-value]


def get_send_bus(project_dir: Path, bus_id: str) -> _SendBus | None:
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT * FROM project_send_buses WHERE id = ?", (bus_id,)
    ).fetchone()
    return _row_to_send_bus(row) if row else None


def list_send_buses(project_dir: Path) -> list[_SendBus]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM project_send_buses ORDER BY order_index"
    ).fetchall()
    return [_row_to_send_bus(r) for r in rows]


def update_send_bus(project_dir: Path, bus_id: str, **fields) -> None:
    if not fields:
        return
    conn = get_db(project_dir)
    sets: list[str] = []
    values: list[Any] = []
    for key, val in fields.items():
        if key == "static_params" and not isinstance(val, str):
            val = json.dumps(val)
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(bus_id)
    conn.execute(f"UPDATE project_send_buses SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def delete_send_bus(project_dir: Path, bus_id: str) -> None:
    """DELETE a bus. ON DELETE CASCADE removes its track_sends rows."""
    conn = get_db(project_dir)
    conn.execute("DELETE FROM project_send_buses WHERE id = ?", (bus_id,))
    conn.commit()


# ── track_sends ───────────────────────────────────────────────────────────

def list_track_sends(
    project_dir: Path,
    track_id: str | None = None,
    bus_id: str | None = None,
) -> list[_TrackSend]:
    """List track_sends rows; optionally filter by track_id and/or bus_id."""
    conn = get_db(project_dir)
    clauses: list[str] = []
    params: list[Any] = []
    if track_id is not None:
        clauses.append("track_id = ?")
        params.append(track_id)
    if bus_id is not None:
        clauses.append("bus_id = ?")
        params.append(bus_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM track_sends {where} ORDER BY track_id, bus_id", params
    ).fetchall()
    return [_row_to_track_send(r) for r in rows]


def get_track_send(
    project_dir: Path, track_id: str, bus_id: str
) -> _TrackSend | None:
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT * FROM track_sends WHERE track_id = ? AND bus_id = ?",
        (track_id, bus_id),
    ).fetchone()
    return _row_to_track_send(row) if row else None


def upsert_track_send(
    project_dir: Path, *, track_id: str, bus_id: str, level: float,
) -> _TrackSend:
    """Insert or update a track_sends row. Composite PK (track_id, bus_id)."""
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO track_sends (track_id, bus_id, level) VALUES (?, ?, ?) "
        "ON CONFLICT(track_id, bus_id) DO UPDATE SET level = excluded.level",
        (track_id, bus_id, float(level)),
    )
    conn.commit()
    return _TrackSend(track_id=track_id, bus_id=bus_id, level=float(level))


def delete_track_send(project_dir: Path, track_id: str, bus_id: str) -> None:
    conn = get_db(project_dir)
    conn.execute(
        "DELETE FROM track_sends WHERE track_id = ? AND bus_id = ?",
        (track_id, bus_id),
    )
    conn.commit()


# ── project_frequency_labels ──────────────────────────────────────────────

def add_frequency_label(
    project_dir: Path, *, label: str, freq_min_hz: float, freq_max_hz: float,
) -> _FrequencyLabel:
    conn = get_db(project_dir)
    lbl_id = generate_id("freq")
    conn.execute(
        "INSERT INTO project_frequency_labels (id, label, freq_min_hz, freq_max_hz) "
        "VALUES (?, ?, ?, ?)",
        (lbl_id, label, float(freq_min_hz), float(freq_max_hz)),
    )
    conn.commit()
    return _FrequencyLabel(
        id=lbl_id, label=label,
        freq_min_hz=float(freq_min_hz), freq_max_hz=float(freq_max_hz),
    )


def get_frequency_label(project_dir: Path, label_id: str) -> _FrequencyLabel | None:
    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT * FROM project_frequency_labels WHERE id = ?", (label_id,)
    ).fetchone()
    return _row_to_frequency_label(row) if row else None


def list_frequency_labels(project_dir: Path) -> list[_FrequencyLabel]:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM project_frequency_labels ORDER BY freq_min_hz"
    ).fetchall()
    return [_row_to_frequency_label(r) for r in rows]


def update_frequency_label(
    project_dir: Path, label_id: str, **fields
) -> None:
    if not fields:
        return
    conn = get_db(project_dir)
    sets: list[str] = []
    values: list[Any] = []
    for key, val in fields.items():
        sets.append(f"{key} = ?")
        values.append(val)
    values.append(label_id)
    conn.execute(
        f"UPDATE project_frequency_labels SET {', '.join(sets)} WHERE id = ?", values
    )
    conn.commit()


def delete_frequency_label(project_dir: Path, label_id: str) -> None:
    conn = get_db(project_dir)
    conn.execute("DELETE FROM project_frequency_labels WHERE id = ?", (label_id,))
    conn.commit()


# ── light_show plugin helpers (M17 MVP) ───────────────────────────────────

# Default hardcoded rig — same 8 fixtures the frontend MVP ships. Seeded on
# first access so fresh projects get a working rig without any user action.
# The frontend reads these back via /plugins/light_show/fixtures and renders
# them in the r3f preview panel. Post-MVP, users author via chat (MCP tools)
# and the backend is source of truth.
_LIGHT_SHOW_DEFAULT_RIG: list[dict] = [
    {"id": "mh_1", "role": "moving_head", "label": "MH 1", "position_x": -3, "position_y": 4, "position_z": 2, "rotation_x": -0.7853981633974483, "rotation_y": 0, "rotation_z": 0},
    {"id": "mh_2", "role": "moving_head", "label": "MH 2", "position_x": -1, "position_y": 4, "position_z": 2, "rotation_x": -0.7853981633974483, "rotation_y": 0, "rotation_z": 0},
    {"id": "mh_3", "role": "moving_head", "label": "MH 3", "position_x":  1, "position_y": 4, "position_z": 2, "rotation_x": -0.7853981633974483, "rotation_y": 0, "rotation_z": 0},
    {"id": "mh_4", "role": "moving_head", "label": "MH 4", "position_x":  3, "position_y": 4, "position_z": 2, "rotation_x": -0.7853981633974483, "rotation_y": 0, "rotation_z": 0},
    {"id": "par_1", "role": "par", "label": "PAR 1", "position_x": -3, "position_y": 2, "position_z": -3, "rotation_x": -0.5235987755982988, "rotation_y": 0, "rotation_z": 0},
    {"id": "par_2", "role": "par", "label": "PAR 2", "position_x": -1, "position_y": 2, "position_z": -3, "rotation_x": -0.5235987755982988, "rotation_y": 0, "rotation_z": 0},
    {"id": "par_3", "role": "par", "label": "PAR 3", "position_x":  1, "position_y": 2, "position_z": -3, "rotation_x": -0.5235987755982988, "rotation_y": 0, "rotation_z": 0},
    {"id": "par_4", "role": "par", "label": "PAR 4", "position_x":  3, "position_y": 2, "position_z": -3, "rotation_x": -0.5235987755982988, "rotation_y": 0, "rotation_z": 0},
]


_FIXTURE_SELECT = (
    "SELECT id, role, label, position_x, position_y, position_z, "
    "rotation_x, rotation_y, rotation_z, "
    "dmx_universe, dmx_address, dmx_channel_count "
    "FROM light_show__fixtures"
)


def list_light_show_fixtures(project_dir: Path) -> list[dict]:
    """List all fixtures in the project's rig, seeding the default rig on
    first access so every project has a working starting point. dmx_*
    fields are nullable — null means 'auto-patch fills the gap'."""
    conn = get_db(project_dir)
    rows = conn.execute(_FIXTURE_SELECT).fetchall()
    if not rows:
        seed_light_show_default_rig(project_dir)
        rows = conn.execute(_FIXTURE_SELECT).fetchall()
    return [dict(r) for r in rows]


def seed_light_show_default_rig(project_dir: Path) -> None:
    """Insert the default 8-fixture rig. No-op if rows already exist."""
    conn = get_db(project_dir)
    existing = conn.execute("SELECT COUNT(*) FROM light_show__fixtures").fetchone()[0]
    if existing > 0:
        return
    for f in _LIGHT_SHOW_DEFAULT_RIG:
        conn.execute(
            "INSERT INTO light_show__fixtures "
            "(id, role, label, position_x, position_y, position_z, "
            " rotation_x, rotation_y, rotation_z) "
            "VALUES (:id, :role, :label, :position_x, :position_y, :position_z, "
            "        :rotation_x, :rotation_y, :rotation_z)",
            f,
        )
    conn.commit()


def upsert_light_show_fixtures(project_dir: Path, fixtures: list[dict]) -> list[dict]:
    """Bulk upsert fixtures with partial-state semantics: omitted fields
    preserve current values. Unknown ``id`` creates a new row. Returns the
    full current rig after the upsert.

    Each input dict MUST have ``id``. Other fields are optional and merge
    against existing row (or default zero/empty for new rows).
    """
    conn = get_db(project_dir)

    def _coerce_int_or_none(v):
        if v is None:
            return None
        return int(v)

    for f in fixtures:
        if "id" not in f or not f["id"]:
            raise ValueError("upsert_light_show_fixtures: each fixture must have an id")
        fid = f["id"]
        # Read existing row for partial-state merge.
        existing = conn.execute(
            "SELECT role, label, position_x, position_y, position_z, "
            "rotation_x, rotation_y, rotation_z, "
            "dmx_universe, dmx_address, dmx_channel_count "
            "FROM light_show__fixtures WHERE id = ?",
            (fid,),
        ).fetchone()
        if existing is None:
            # New fixture — default zero/empty for any unspecified field.
            # dmx_* default to NULL (auto-patcher will fill) unless caller
            # specifies them.
            row = {
                "id": fid,
                "role": f.get("role", "par"),
                "label": f.get("label", fid),
                "position_x": float(f.get("position_x", 0)),
                "position_y": float(f.get("position_y", 0)),
                "position_z": float(f.get("position_z", 0)),
                "rotation_x": float(f.get("rotation_x", 0)),
                "rotation_y": float(f.get("rotation_y", 0)),
                "rotation_z": float(f.get("rotation_z", 0)),
                "dmx_universe": _coerce_int_or_none(f.get("dmx_universe")),
                "dmx_address": _coerce_int_or_none(f.get("dmx_address")),
                "dmx_channel_count": _coerce_int_or_none(f.get("dmx_channel_count")),
            }
            conn.execute(
                "INSERT INTO light_show__fixtures "
                "(id, role, label, position_x, position_y, position_z, "
                " rotation_x, rotation_y, rotation_z, "
                " dmx_universe, dmx_address, dmx_channel_count) "
                "VALUES (:id, :role, :label, :position_x, :position_y, :position_z, "
                "        :rotation_x, :rotation_y, :rotation_z, "
                "        :dmx_universe, :dmx_address, :dmx_channel_count)",
                row,
            )
        else:
            # Existing — merge partial fields. ``in`` (rather than ``get``)
            # for dmx_* so callers can explicitly clear back to null by
            # passing ``None``; omitting the key preserves current value.
            existing_d = dict(existing)
            row = {
                "id": fid,
                "role": f.get("role", existing_d["role"]),
                "label": f.get("label", existing_d["label"]),
                "position_x": float(f.get("position_x", existing_d["position_x"])),
                "position_y": float(f.get("position_y", existing_d["position_y"])),
                "position_z": float(f.get("position_z", existing_d["position_z"])),
                "rotation_x": float(f.get("rotation_x", existing_d["rotation_x"])),
                "rotation_y": float(f.get("rotation_y", existing_d["rotation_y"])),
                "rotation_z": float(f.get("rotation_z", existing_d["rotation_z"])),
                "dmx_universe": _coerce_int_or_none(f["dmx_universe"]) if "dmx_universe" in f else existing_d["dmx_universe"],
                "dmx_address": _coerce_int_or_none(f["dmx_address"]) if "dmx_address" in f else existing_d["dmx_address"],
                "dmx_channel_count": _coerce_int_or_none(f["dmx_channel_count"]) if "dmx_channel_count" in f else existing_d["dmx_channel_count"],
            }
            conn.execute(
                "UPDATE light_show__fixtures SET "
                "role=:role, label=:label, "
                "position_x=:position_x, position_y=:position_y, position_z=:position_z, "
                "rotation_x=:rotation_x, rotation_y=:rotation_y, rotation_z=:rotation_z, "
                "dmx_universe=:dmx_universe, dmx_address=:dmx_address, "
                "dmx_channel_count=:dmx_channel_count, "
                "updated_at=datetime('now') "
                "WHERE id=:id",
                row,
            )
    conn.commit()
    return list_light_show_fixtures(project_dir)


def reset_light_show_fixtures(project_dir: Path) -> list[dict]:
    """Delete all fixtures, then re-seed the default rig. Returns the
    post-reset rig."""
    conn = get_db(project_dir)
    conn.execute("DELETE FROM light_show__fixtures")
    conn.commit()
    seed_light_show_default_rig(project_dir)
    return list_light_show_fixtures(project_dir)


def remove_light_show_fixtures(project_dir: Path, ids: list[str]) -> list[dict]:
    """Delete specific fixtures by id. Returns the rig after removal.
    Missing ids are silently ignored."""
    conn = get_db(project_dir)
    for fid in ids:
        conn.execute("DELETE FROM light_show__fixtures WHERE id = ?", (fid,))
    conn.commit()
    return list_light_show_fixtures(project_dir)


# ── light_show channel overrides ───────────────────────────────────────────


def list_light_show_overrides(project_dir: Path) -> list[dict]:
    """Return current overrides as a list. Each entry has ``fixture_id`` and
    zero or more channel keys (``intensity``, ``color`` [r,g,b], ``pan``,
    ``tilt``) — keys are present only when the channel is overridden."""
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT fixture_id, intensity, color_r, color_g, color_b, pan, tilt "
        "FROM light_show__overrides"
    ).fetchall()
    out = []
    for r in rows:
        entry: dict = {"fixture_id": r["fixture_id"]}
        if r["intensity"] is not None:
            entry["intensity"] = r["intensity"]
        if r["color_r"] is not None:
            entry["color"] = [r["color_r"], r["color_g"], r["color_b"]]
        if r["pan"] is not None:
            entry["pan"] = r["pan"]
        if r["tilt"] is not None:
            entry["tilt"] = r["tilt"]
        out.append(entry)
    return out


def set_light_show_overrides(project_dir: Path, overrides: list[dict]) -> list[dict]:
    """Bulk upsert overrides with partial-field semantics.

    Each input dict MUST have ``id`` (the fixture id). Optional keys:
      - ``intensity`` (float 0..1)
      - ``color`` ([r, g, b] each 0..1) — all three or none
      - ``pan`` (radians, signed)
      - ``tilt`` (radians, signed)

    Omitted keys on an existing override row are preserved (still NULL or
    still set, whichever they were). To clear a channel back to scene-driven,
    use ``clear_light_show_overrides`` with the specific fixture_id (clears
    all channels for that fixture) — per-channel clear is TODO if needed.

    Returns the full override list after upsert.
    """
    conn = get_db(project_dir)
    for o in overrides:
        fid = o.get("id") or o.get("fixture_id")
        if not fid:
            raise ValueError("set_light_show_overrides: each entry must have an id")

        # Verify the fixture exists — overrides are FK'd to fixtures.
        exists = conn.execute(
            "SELECT 1 FROM light_show__fixtures WHERE id = ?", (fid,)
        ).fetchone()
        if not exists:
            raise ValueError(f"fixture {fid!r} does not exist")

        # Partial merge against the existing override row, if any.
        existing = conn.execute(
            "SELECT intensity, color_r, color_g, color_b, pan, tilt "
            "FROM light_show__overrides WHERE fixture_id = ?",
            (fid,),
        ).fetchone()
        row = {
            "fixture_id": fid,
            "intensity": existing["intensity"] if existing else None,
            "color_r": existing["color_r"] if existing else None,
            "color_g": existing["color_g"] if existing else None,
            "color_b": existing["color_b"] if existing else None,
            "pan": existing["pan"] if existing else None,
            "tilt": existing["tilt"] if existing else None,
        }

        if "intensity" in o:
            row["intensity"] = None if o["intensity"] is None else float(o["intensity"])
        if "color" in o:
            c = o["color"]
            if c is None:
                row["color_r"] = row["color_g"] = row["color_b"] = None
            else:
                if not isinstance(c, (list, tuple)) or len(c) != 3:
                    raise ValueError(f"color must be [r, g, b], got {c!r}")
                row["color_r"], row["color_g"], row["color_b"] = (float(c[0]), float(c[1]), float(c[2]))
        if "pan" in o:
            row["pan"] = None if o["pan"] is None else float(o["pan"])
        if "tilt" in o:
            row["tilt"] = None if o["tilt"] is None else float(o["tilt"])

        conn.execute(
            "INSERT INTO light_show__overrides "
            "(fixture_id, intensity, color_r, color_g, color_b, pan, tilt) "
            "VALUES (:fixture_id, :intensity, :color_r, :color_g, :color_b, :pan, :tilt) "
            "ON CONFLICT(fixture_id) DO UPDATE SET "
            "intensity=:intensity, color_r=:color_r, color_g=:color_g, color_b=:color_b, "
            "pan=:pan, tilt=:tilt, updated_at=datetime('now')",
            row,
        )
    conn.commit()
    return list_light_show_overrides(project_dir)


def clear_light_show_overrides(project_dir: Path, fixture_ids: list[str] | None = None) -> list[dict]:
    """Clear overrides. If ``fixture_ids`` is None or empty, clears ALL
    overrides. Otherwise clears only the specified fixtures. Returns the
    remaining override list."""
    conn = get_db(project_dir)
    if not fixture_ids:
        conn.execute("DELETE FROM light_show__overrides")
    else:
        for fid in fixture_ids:
            conn.execute("DELETE FROM light_show__overrides WHERE fixture_id = ?", (fid,))
    conn.commit()
    return list_light_show_overrides(project_dir)


# ── light_show video screens ──────────────────────────────────────────────


_SCREEN_SELECT = (
    "SELECT id, label, position_x, position_y, position_z, "
    "rotation_x, rotation_y, rotation_z, width, height "
    "FROM light_show__screens ORDER BY id"
)


def list_light_show_screens(project_dir: Path) -> list[dict]:
    """List all screens in the project. Empty by default — screens are
    opt-in via chat, unlike fixtures which seed a default rig."""
    conn = get_db(project_dir)
    rows = conn.execute(_SCREEN_SELECT).fetchall()
    return [dict(r) for r in rows]


def upsert_light_show_screens(project_dir: Path, screens: list[dict]) -> list[dict]:
    """Bulk upsert screens with partial-state semantics: omitted fields
    preserve current values. Unknown ``id`` creates a new row (defaulting
    size to 4x2.25m at the origin). Returns the full screen list after
    the upsert."""
    conn = get_db(project_dir)
    for s in screens:
        if "id" not in s or not s["id"]:
            raise ValueError("upsert_light_show_screens: each screen must have an id")
        sid = s["id"]
        existing = conn.execute(
            "SELECT label, position_x, position_y, position_z, "
            "rotation_x, rotation_y, rotation_z, width, height "
            "FROM light_show__screens WHERE id = ?",
            (sid,),
        ).fetchone()
        if existing is None:
            row = {
                "id": sid,
                "label": s.get("label", sid),
                "position_x": float(s.get("position_x", 0)),
                "position_y": float(s.get("position_y", 0)),
                "position_z": float(s.get("position_z", 0)),
                "rotation_x": float(s.get("rotation_x", 0)),
                "rotation_y": float(s.get("rotation_y", 0)),
                "rotation_z": float(s.get("rotation_z", 0)),
                "width": float(s.get("width", 4.0)),
                "height": float(s.get("height", 2.25)),
            }
            conn.execute(
                "INSERT INTO light_show__screens "
                "(id, label, position_x, position_y, position_z, "
                " rotation_x, rotation_y, rotation_z, width, height) "
                "VALUES (:id, :label, :position_x, :position_y, :position_z, "
                "        :rotation_x, :rotation_y, :rotation_z, :width, :height)",
                row,
            )
        else:
            ex = dict(existing)
            row = {
                "id": sid,
                "label": s.get("label", ex["label"]),
                "position_x": float(s.get("position_x", ex["position_x"])),
                "position_y": float(s.get("position_y", ex["position_y"])),
                "position_z": float(s.get("position_z", ex["position_z"])),
                "rotation_x": float(s.get("rotation_x", ex["rotation_x"])),
                "rotation_y": float(s.get("rotation_y", ex["rotation_y"])),
                "rotation_z": float(s.get("rotation_z", ex["rotation_z"])),
                "width": float(s.get("width", ex["width"])),
                "height": float(s.get("height", ex["height"])),
            }
            conn.execute(
                "UPDATE light_show__screens SET "
                "label=:label, "
                "position_x=:position_x, position_y=:position_y, position_z=:position_z, "
                "rotation_x=:rotation_x, rotation_y=:rotation_y, rotation_z=:rotation_z, "
                "width=:width, height=:height, updated_at=datetime('now') "
                "WHERE id=:id",
                row,
            )
    conn.commit()
    return list_light_show_screens(project_dir)


def remove_light_show_screens(project_dir: Path, ids: list[str]) -> list[dict]:
    """Delete specific screens by id. Missing ids are silently ignored.
    Returns the remaining screen list."""
    conn = get_db(project_dir)
    for sid in ids:
        conn.execute("DELETE FROM light_show__screens WHERE id = ?", (sid,))
    conn.commit()
    return list_light_show_screens(project_dir)


def reset_light_show_screens(project_dir: Path) -> list[dict]:
    """Delete ALL screens. Unlike ``reset_light_show_fixtures``, this does
    not re-seed defaults — screens start empty and are authored per project."""
    conn = get_db(project_dir)
    conn.execute("DELETE FROM light_show__screens")
    conn.commit()
    return list_light_show_screens(project_dir)


# ── M19 Light Show Scene Editor — DB helpers ──────────────────────────────
#
# Three helpers per resource (list / upsert / remove) plus three for the
# single-row live override (get / activate / deactivate). All sparse-params
# semantics, atomic batches, and FK/CHECK enforcement live here so the
# REST + MCP layers above can be thin dispatchers.


_SCENE_SELECT = (
    "SELECT id, label, type, params_json, created_at, updated_at "
    "FROM light_show__scenes"
)


def _scene_row_to_dict(r) -> dict:
    """Decode params_json into a sparse dict, leave everything else as-is."""
    d = dict(r)
    raw = d.pop("params_json", None)
    try:
        d["params"] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        d["params"] = {}
    return d


def list_light_show_scenes(
    project_dir: Path,
    *,
    ids: list[str] | None = None,
    type_filter: str | None = None,
    label_query: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "updated_at",
    order: str = "desc",
) -> tuple[list[dict], int, bool]:
    """List scenes with optional filtering. Returns (rows, total, has_more).

    ``params`` field is the SPARSE stored value — only keys explicitly
    overridden by the user. Catalog defaults are merged at evaluator time
    (not here) so list → set round-trips don't promote defaults to overrides.
    Spec R5.
    """
    if order_by not in ("updated_at", "created_at", "label"):
        raise ValueError(f"order_by must be updated_at|created_at|label, got {order_by!r}")
    if order not in ("asc", "desc"):
        raise ValueError(f"order must be asc|desc, got {order!r}")
    limit = max(0, min(int(limit), 1000))
    offset = max(0, int(offset))

    conn = get_db(project_dir)
    where: list[str] = []
    params: list = []
    if ids:
        placeholders = ",".join(["?"] * len(ids))
        where.append(f"id IN ({placeholders})")
        params.extend(ids)
    if type_filter:
        where.append("type = ?")
        params.append(type_filter)
    if label_query:
        where.append("LOWER(label) LIKE LOWER(?)")
        params.append(f"%{label_query}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total_row = conn.execute(
        f"SELECT COUNT(*) FROM light_show__scenes{clause}", params
    ).fetchone()
    total = int(total_row[0])

    sql = f"{_SCENE_SELECT}{clause} ORDER BY {order_by} {order.upper()} LIMIT ? OFFSET ?"
    rows = conn.execute(sql, [*params, limit, offset]).fetchall()
    out = [_scene_row_to_dict(r) for r in rows]
    has_more = (offset + len(out)) < total
    return out, total, has_more


def _validate_scene_type(conn: sqlite3.Connection, type_name: str) -> None:
    """Type-name validation against the primitives catalog. Catalog access
    deferred to caller — backend reads catalog YAML once and passes the
    set of known types into the upsert path. Local helper here just trusts
    the caller has already filtered, so DB enforces shape only."""
    # Intentional pass-through. Catalog enforcement happens at the MCP /
    # REST layer (task-154) where the catalog is loaded once. The DB layer
    # accepts any string and only enforces FK + CHECK constraints.
    del conn, type_name


def upsert_light_show_scenes(
    project_dir: Path,
    scenes: list[dict],
    *,
    known_types: set[str] | None = None,
) -> list[dict]:
    """Bulk upsert scenes with id-presence dispatch and sparse params.

    Behavior per spec R6/R7/R8:
      - No ``id`` on entry → CREATE with server-generated UUID. ``label``
        and ``type`` MUST be present (NOT NULL columns); rejected otherwise.
      - ``id`` present → UPDATE existing row. ``label`` / ``type`` partial
        merge (omitted preserves, value sets, null rejected). ``params``
        merge per RFC 7396 JSON Merge Patch:
          - omitted → preserve all stored params
          - ``null`` → reject (caller must use ``{}`` to preserve, ``{key: null}`` to delete)
          - ``{key: value}`` → set
          - ``{key: null}`` → delete the key from stored params
      - Atomic all-or-nothing across the batch (single transaction; any
        rejection rolls back all writes).
      - Storage is sparse — params_json contains ONLY explicitly-set keys.

    ``known_types`` (optional): set of valid primitive type names. When
    provided, entries with type not in the set are rejected. Pass ``None``
    to skip type validation (legacy caller / testing).

    Returns the list of upserted rows in input order with sparse params.
    """
    if not isinstance(scenes, list):
        raise ValueError("scenes must be a list")

    conn = get_db(project_dir)
    out: list[dict] = []
    try:
        conn.execute("BEGIN")
        for entry in scenes:
            if not isinstance(entry, dict):
                raise ValueError("each scene must be a dict")
            if "id" in entry and entry["id"] is not None:
                # UPDATE path
                sid = str(entry["id"])
                existing = conn.execute(
                    "SELECT id, label, type, params_json FROM light_show__scenes WHERE id = ?",
                    (sid,),
                ).fetchone()
                if existing is None:
                    raise ValueError(f"unknown scene id: {sid}")

                # label
                if "label" in entry:
                    if entry["label"] is None:
                        raise ValueError("cannot null required column: label")
                    new_label = str(entry["label"])
                else:
                    new_label = existing["label"]

                # type
                if "type" in entry:
                    if entry["type"] is None:
                        raise ValueError("cannot null required column: type")
                    new_type = str(entry["type"])
                else:
                    new_type = existing["type"]
                if known_types is not None and new_type not in known_types:
                    raise ValueError(f"unknown primitive type: {new_type}")

                # params merge-patch (sparse)
                stored = json.loads(existing["params_json"] or "{}")
                if "params" in entry:
                    p = entry["params"]
                    if p is None:
                        raise ValueError(
                            "params object cannot be null; use {} to preserve "
                            "or {key: null} to delete a key"
                        )
                    if not isinstance(p, dict):
                        raise ValueError("params must be an object")
                    for k, v in p.items():
                        if v is None:
                            stored.pop(k, None)
                        else:
                            stored[k] = v
                merged_params_json = json.dumps(stored)

                conn.execute(
                    "UPDATE light_show__scenes SET label=?, type=?, params_json=?, "
                    "updated_at=datetime('now') WHERE id=?",
                    (new_label, new_type, merged_params_json, sid),
                )
                row = conn.execute(_SCENE_SELECT + " WHERE id = ?", (sid,)).fetchone()
                out.append(_scene_row_to_dict(row))
            else:
                # CREATE path
                if "label" not in entry or entry.get("label") is None:
                    raise ValueError("label and type required to create scene")
                if "type" not in entry or entry.get("type") is None:
                    raise ValueError("label and type required to create scene")
                new_type = str(entry["type"])
                if known_types is not None and new_type not in known_types:
                    raise ValueError(f"unknown primitive type: {new_type}")
                params = entry.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                # Sparse: store exactly what's supplied; null values dropped on insert
                # since semantically null means 'delete' which is a no-op on a fresh row.
                params = {k: v for k, v in params.items() if v is not None}
                new_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO light_show__scenes (id, label, type, params_json) "
                    "VALUES (?, ?, ?, ?)",
                    (new_id, str(entry["label"]), new_type, json.dumps(params)),
                )
                row = conn.execute(_SCENE_SELECT + " WHERE id = ?", (new_id,)).fetchone()
                out.append(_scene_row_to_dict(row))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def remove_light_show_scenes(project_dir: Path, ids: list[str]) -> list[dict]:
    """Delete scenes atomically. Blocked when any id is referenced by a
    placement OR held by the live override.

    Spec R9: blocked-by-placements returns ``{error, blocked: [{scene_id, placement_ids}, ...]}``
    Spec R10: blocked-by-live returns ``{error, blocked_by_live: scene_id}``
    Caller (REST/MCP) translates ValueError messages into the structured error
    shape — this function raises ValueError with structured details on the args.

    Missing ids are silently skipped on success (no error). Returns the
    deleted rows (pre-deletion state) on success.
    """
    if not isinstance(ids, list):
        raise ValueError("ids must be a list")
    ids = [str(i) for i in ids if i]
    if not ids:
        return []

    conn = get_db(project_dir)
    try:
        conn.execute("BEGIN")
        # Block-by-live check
        live = conn.execute(
            "SELECT scene_id FROM light_show__live_override WHERE scene_id IS NOT NULL"
        ).fetchone()
        if live is not None and live["scene_id"] in ids:
            raise BlockedByLiveError(live["scene_id"])

        # Block-by-placements check — collect ALL blocked scenes for the report
        placeholders = ",".join(["?"] * len(ids))
        place_rows = conn.execute(
            f"SELECT scene_id, id AS placement_id FROM light_show__scene_placements "
            f"WHERE scene_id IN ({placeholders})",
            ids,
        ).fetchall()
        if place_rows:
            blocked: dict[str, list[str]] = {}
            for r in place_rows:
                blocked.setdefault(r["scene_id"], []).append(r["placement_id"])
            raise BlockedByPlacementsError(
                [{"scene_id": sid, "placement_ids": pids} for sid, pids in blocked.items()]
            )

        # Capture pre-deletion rows then delete
        existing = conn.execute(
            f"SELECT id, label, type, params_json, created_at, updated_at "
            f"FROM light_show__scenes WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        deleted = [_scene_row_to_dict(r) for r in existing]
        if existing:
            conn.execute(
                f"DELETE FROM light_show__scenes WHERE id IN ({placeholders})",
                ids,
            )
        conn.execute("COMMIT")
        return deleted
    except Exception:
        conn.execute("ROLLBACK")
        raise


class BlockedByLiveError(ValueError):
    """Scene cannot be removed because the live override holds it."""

    def __init__(self, scene_id: str):
        super().__init__(f"scene held by live override; deactivate first: {scene_id}")
        self.scene_id = scene_id


class BlockedByPlacementsError(ValueError):
    """One or more scenes cannot be removed because placements reference them."""

    def __init__(self, blocked: list[dict]):
        super().__init__(f"scene(s) still referenced by placements: {blocked}")
        self.blocked = blocked


# ── Placements ────────────────────────────────────────────────────────────


_PLACEMENT_SELECT = (
    "SELECT id, scene_id, start_time, end_time, display_order, "
    "fade_in_sec, fade_out_sec, created_at, updated_at "
    "FROM light_show__scene_placements"
)


def list_light_show_placements(
    project_dir: Path,
    *,
    ids: list[str] | None = None,
    scene_id: str | None = None,
    time_start: float | None = None,
    time_end: float | None = None,
    limit: int = 100,
    offset: int = 0,
    order_by: str = "start_time",
    order: str = "asc",
) -> tuple[list[dict], int, bool]:
    """List placements with optional filtering. Returns (rows, total, has_more).

    ``time_start`` and ``time_end`` filter to placements that OVERLAP the window
    — i.e. ``placement.start_time <= time_end AND placement.end_time >= time_start``.
    Spec R12.
    """
    if order_by not in ("start_time", "created_at"):
        raise ValueError(f"order_by must be start_time|created_at, got {order_by!r}")
    if order not in ("asc", "desc"):
        raise ValueError(f"order must be asc|desc, got {order!r}")
    limit = max(0, min(int(limit), 1000))
    offset = max(0, int(offset))

    conn = get_db(project_dir)
    where: list[str] = []
    params: list = []
    if ids:
        placeholders = ",".join(["?"] * len(ids))
        where.append(f"id IN ({placeholders})")
        params.extend(ids)
    if scene_id:
        where.append("scene_id = ?")
        params.append(scene_id)
    if time_start is not None and time_end is not None:
        # Overlap with [time_start, time_end]
        where.append("start_time <= ? AND end_time >= ?")
        params.append(float(time_end))
        params.append(float(time_start))
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = int(conn.execute(
        f"SELECT COUNT(*) FROM light_show__scene_placements{clause}", params
    ).fetchone()[0])
    rows = conn.execute(
        f"{_PLACEMENT_SELECT}{clause} ORDER BY {order_by} {order.upper()} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    out = [dict(r) for r in rows]
    has_more = (offset + len(out)) < total
    return out, total, has_more


def upsert_light_show_placements(
    project_dir: Path,
    placements: list[dict],
) -> list[dict]:
    """Bulk upsert placements with id-presence dispatch.

    Spec R13: missing id → INSERT new uuid; present id → UPDATE merge.
    Spec R14: rejects ``end_time <= start_time`` atomically.
    Spec R15: rejects unknown ``scene_id`` atomically.

    Returns the list of upserted rows in input order.
    """
    if not isinstance(placements, list):
        raise ValueError("placements must be a list")

    conn = get_db(project_dir)
    out: list[dict] = []
    try:
        conn.execute("BEGIN")
        for entry in placements:
            if not isinstance(entry, dict):
                raise ValueError("each placement must be a dict")
            pid = entry.get("id")
            if pid:
                # UPDATE merge
                pid = str(pid)
                existing = conn.execute(
                    "SELECT id, scene_id, start_time, end_time, display_order, "
                    "fade_in_sec, fade_out_sec FROM light_show__scene_placements WHERE id = ?",
                    (pid,),
                ).fetchone()
                if existing is None:
                    raise ValueError(f"unknown placement id: {pid}")
                merged = dict(existing)
                for k in ("scene_id", "start_time", "end_time", "display_order", "fade_in_sec", "fade_out_sec"):
                    if k in entry and entry[k] is not None:
                        merged[k] = entry[k]
                # Validate post-merge state.
                if float(merged["end_time"]) <= float(merged["start_time"]):
                    raise ValueError("placement end_time must be greater than start_time")
                # FK is enforced at INSERT/UPDATE time by SQLite once PRAGMA foreign_keys
                # is on. Belt-and-suspenders: explicit check for the error message shape.
                exists = conn.execute(
                    "SELECT 1 FROM light_show__scenes WHERE id = ?",
                    (str(merged["scene_id"]),),
                ).fetchone()
                if not exists:
                    raise ValueError(f"unknown scene_id: {merged['scene_id']}")
                conn.execute(
                    "UPDATE light_show__scene_placements SET "
                    "scene_id=?, start_time=?, end_time=?, display_order=?, "
                    "fade_in_sec=?, fade_out_sec=?, updated_at=datetime('now') "
                    "WHERE id=?",
                    (
                        str(merged["scene_id"]),
                        float(merged["start_time"]),
                        float(merged["end_time"]),
                        int(merged["display_order"]),
                        float(merged["fade_in_sec"]),
                        float(merged["fade_out_sec"]),
                        pid,
                    ),
                )
            else:
                # INSERT
                for required in ("scene_id", "start_time", "end_time"):
                    if required not in entry or entry[required] is None:
                        raise ValueError(f"placement missing required field: {required}")
                if float(entry["end_time"]) <= float(entry["start_time"]):
                    raise ValueError("placement end_time must be greater than start_time")
                exists = conn.execute(
                    "SELECT 1 FROM light_show__scenes WHERE id = ?",
                    (str(entry["scene_id"]),),
                ).fetchone()
                if not exists:
                    raise ValueError(f"unknown scene_id: {entry['scene_id']}")
                pid = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO light_show__scene_placements "
                    "(id, scene_id, start_time, end_time, display_order, fade_in_sec, fade_out_sec) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        pid,
                        str(entry["scene_id"]),
                        float(entry["start_time"]),
                        float(entry["end_time"]),
                        int(entry.get("display_order", 0)),
                        float(entry.get("fade_in_sec", 0)),
                        float(entry.get("fade_out_sec", 0)),
                    ),
                )
            row = conn.execute(_PLACEMENT_SELECT + " WHERE id = ?", (pid,)).fetchone()
            out.append(dict(row))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def remove_light_show_placements(project_dir: Path, ids: list[str]) -> list[dict]:
    """Delete placements by id. Silently ignores missing ids; returns the
    deleted rows (pre-deletion state). Spec R16."""
    if not isinstance(ids, list):
        raise ValueError("ids must be a list")
    ids = [str(i) for i in ids if i]
    if not ids:
        return []
    conn = get_db(project_dir)
    placeholders = ",".join(["?"] * len(ids))
    rows = conn.execute(
        f"{_PLACEMENT_SELECT} WHERE id IN ({placeholders})", ids
    ).fetchall()
    deleted = [dict(r) for r in rows]
    if rows:
        conn.execute(
            f"DELETE FROM light_show__scene_placements WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
    return deleted


# ── Live override ─────────────────────────────────────────────────────────


def _live_override_row_to_dict(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    if d.get("inline_params_json"):
        try:
            d["inline_params"] = json.loads(d["inline_params_json"])
        except json.JSONDecodeError:
            d["inline_params"] = {}
    d.pop("inline_params_json", None)
    return d


def get_light_show_live_override(project_dir: Path) -> dict | None:
    """Return the single-row live override (if active) or None.
    Spec foundation for R26."""
    conn = get_db(project_dir)
    r = conn.execute(
        "SELECT id, scene_id, inline_type, inline_params_json, label, "
        "fade_in_sec, fade_out_sec, activated_at, deactivation_started_at "
        "FROM light_show__live_override WHERE id = 'current'"
    ).fetchone()
    return _live_override_row_to_dict(r)


def activate_light_show_live_override(
    project_dir: Path,
    payload: dict,
    *,
    known_types: set[str] | None = None,
) -> dict:
    """Activate (or replace) the live override.

    Payload shape (per spec R18-R23):
      - Either ``scene_id: <uuid>`` (library reference) OR
        ``scene: {type, params?}`` (inline directive). Both/neither rejected.
      - Optional ``label``, ``fade_in_sec`` (default 0).
      - Optional ``save_as: <label>`` — inline only. When present, persists
        the inline scene into ``light_show__scenes`` first, then references
        it by id. Rejects ``save_as`` without inline ``scene``.

    Replaces any existing override silently. Returns the resulting status
    shape (same as get_light_show_live_override).
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    has_scene_id = "scene_id" in payload and payload["scene_id"]
    inline = payload.get("scene")
    has_inline = isinstance(inline, dict) and "type" in inline
    if has_scene_id and has_inline:
        raise ValueError("provide scene_id OR scene, not both")
    if not has_scene_id and not has_inline:
        raise ValueError("provide scene_id OR scene")

    save_as = payload.get("save_as")
    if save_as is not None and not has_inline:
        raise ValueError("save_as requires inline scene")

    fade_in = float(payload.get("fade_in_sec", 0))
    label = payload.get("label")

    conn = get_db(project_dir)
    try:
        conn.execute("BEGIN")

        if has_scene_id:
            sid = str(payload["scene_id"])
            scene = conn.execute(
                "SELECT id, label, type FROM light_show__scenes WHERE id = ?",
                (sid,),
            ).fetchone()
            if scene is None:
                raise ValueError(f"unknown scene_id: {sid}")
            row_label = label if label is not None else scene["label"]
            conn.execute("DELETE FROM light_show__live_override WHERE id = 'current'")
            conn.execute(
                "INSERT INTO light_show__live_override "
                "(id, scene_id, inline_type, inline_params_json, label, fade_in_sec, "
                " fade_out_sec, activated_at, deactivation_started_at) "
                "VALUES ('current', ?, NULL, NULL, ?, ?, 0, datetime('now'), NULL)",
                (sid, str(row_label), fade_in),
            )
        else:
            # Inline directive
            inline_type = str(inline["type"])
            if known_types is not None and inline_type not in known_types:
                raise ValueError(f"unknown primitive type: {inline_type}")
            inline_params = inline.get("params") or {}
            if not isinstance(inline_params, dict):
                raise ValueError("inline scene.params must be an object")
            inline_params = {k: v for k, v in inline_params.items() if v is not None}

            if save_as is not None:
                # Persist as a library scene first, then reference it
                new_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO light_show__scenes (id, label, type, params_json) "
                    "VALUES (?, ?, ?, ?)",
                    (new_id, str(save_as), inline_type, json.dumps(inline_params)),
                )
                row_label = label if label is not None else str(save_as)
                conn.execute("DELETE FROM light_show__live_override WHERE id = 'current'")
                conn.execute(
                    "INSERT INTO light_show__live_override "
                    "(id, scene_id, inline_type, inline_params_json, label, fade_in_sec, "
                    " fade_out_sec, activated_at, deactivation_started_at) "
                    "VALUES ('current', ?, NULL, NULL, ?, ?, 0, datetime('now'), NULL)",
                    (new_id, str(row_label), fade_in),
                )
            else:
                row_label = label if label is not None else "directive"
                conn.execute("DELETE FROM light_show__live_override WHERE id = 'current'")
                conn.execute(
                    "INSERT INTO light_show__live_override "
                    "(id, scene_id, inline_type, inline_params_json, label, fade_in_sec, "
                    " fade_out_sec, activated_at, deactivation_started_at) "
                    "VALUES ('current', NULL, ?, ?, ?, ?, 0, datetime('now'), NULL)",
                    (inline_type, json.dumps(inline_params), str(row_label), fade_in),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return get_light_show_live_override(project_dir) or {}


def deactivate_light_show_live_override(
    project_dir: Path,
    fade_out_sec: float = 0,
) -> dict:
    """Mark the live override as deactivating. Sets ``deactivation_started_at``
    and updates ``fade_out_sec``. Physical row deletion happens later (the
    frontend evaluator triggers when fade-out completes — task-161).

    Spec R24/R25: when no override is active, returns ``{active: False}``
    (no-op). When active, returns the post-update status row.
    """
    conn = get_db(project_dir)
    existing = conn.execute(
        "SELECT id FROM light_show__live_override WHERE id = 'current'"
    ).fetchone()
    if existing is None:
        return {"active": False}
    conn.execute(
        "UPDATE light_show__live_override SET "
        "fade_out_sec = ?, deactivation_started_at = datetime('now') "
        "WHERE id = 'current'",
        (float(fade_out_sec),),
    )
    conn.commit()
    return get_light_show_live_override(project_dir) or {"active": False}
