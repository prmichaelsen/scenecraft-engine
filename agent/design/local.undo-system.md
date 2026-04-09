# Undo System

**Concept**: Hybrid SQLite trigger + command pattern undo/redo for timeline editing operations  
**Created**: 2026-04-09  
**Status**: Design Specification  

---

## Overview

Persistent undo/redo for all timeline editing operations in SceneCraft. Uses SQLite triggers to automatically capture inverse SQL for every DB change, with a command layer for operation grouping and human-readable descriptions. Undo history persists across sessions in project.db.

---

## Problem Statement

- No undo capability — accidental edits, bulk operations, and split/merge mistakes require manual DB restoration from backups
- 353 corrupt transitions were cleaned up in one session — could have been prevented with undo
- Users frequently need to experiment and revert timeline changes

---

## Solution

### Architecture: Hybrid Trigger + Command Pattern

**Layer 1 — SQLite Triggers** (automatic):
- INSERT trigger → logs `DELETE` inverse
- UPDATE trigger → logs `UPDATE` with all old column values
- DELETE trigger → logs `INSERT` with all old column values
- All inverse SQL written to `undo_log` table with `undo_group` ID
- `undo_state` table controls trigger activation (disabled during undo replay)

**Layer 2 — Command Layer** (explicit):
- Before each API operation: increment `undo_group`
- After operation: record description in `undo_groups` table
- Provides human-readable history for UI

**Undo execution**:
1. Disable triggers (`undo_state.active = 0`)
2. Execute inverse SQL for the group in reverse `seq` order
3. Delete the undo_log entries for that group
4. Re-enable triggers

---

## Implementation

### Database Schema

```sql
-- Undo log: inverse SQL statements captured by triggers
CREATE TABLE IF NOT EXISTS undo_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    undo_group INTEGER NOT NULL,
    sql_text TEXT NOT NULL
);

-- Undo group metadata: human-readable descriptions
CREATE TABLE IF NOT EXISTS undo_groups (
    id INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    undone INTEGER DEFAULT 0
);

-- Trigger control state
CREATE TABLE IF NOT EXISTS undo_state (
    key TEXT PRIMARY KEY,
    value INTEGER
);
-- Initialize: INSERT OR IGNORE INTO undo_state VALUES ('current_group', 0);
-- Initialize: INSERT OR IGNORE INTO undo_state VALUES ('active', 1);
```

### Trigger Template (per table)

For each tracked table (keyframes, transitions, suppressions, effects):

```sql
-- INSERT → log DELETE
CREATE TRIGGER IF NOT EXISTS {table}_insert_undo AFTER INSERT ON {table}
WHEN (SELECT value FROM undo_state WHERE key='active') = 1
BEGIN
    INSERT INTO undo_log (undo_group, sql_text)
    VALUES (
        (SELECT value FROM undo_state WHERE key='current_group'),
        'DELETE FROM {table} WHERE {pk}=' || quote(NEW.{pk})
    );
END;

-- UPDATE → log UPDATE with old values
CREATE TRIGGER IF NOT EXISTS {table}_update_undo AFTER UPDATE ON {table}
WHEN (SELECT value FROM undo_state WHERE key='active') = 1
BEGIN
    INSERT INTO undo_log (undo_group, sql_text)
    VALUES (
        (SELECT value FROM undo_state WHERE key='current_group'),
        'UPDATE {table} SET {col1}=' || quote(OLD.{col1})
        || ',{col2}=' || quote(OLD.{col2})
        || ... 
        || ' WHERE {pk}=' || quote(OLD.{pk})
    );
END;

-- DELETE → log INSERT with old values
CREATE TRIGGER IF NOT EXISTS {table}_delete_undo BEFORE DELETE ON {table}
WHEN (SELECT value FROM undo_state WHERE key='active') = 1
BEGIN
    INSERT INTO undo_log (undo_group, sql_text)
    VALUES (
        (SELECT value FROM undo_state WHERE key='current_group'),
        'INSERT INTO {table} ({pk},{col1},{col2},...) VALUES ('
        || quote(OLD.{pk}) || ',' || quote(OLD.{col1}) || ',' || quote(OLD.{col2})
        || ',...)'
    );
END;
```

### Python API (db.py)

```python
def undo_begin(project_dir: Path, description: str) -> int:
    """Start a new undo group. Call before an API operation."""
    conn = get_db(project_dir)
    conn.execute("UPDATE undo_state SET value = value + 1 WHERE key = 'current_group'")
    group_id = conn.execute("SELECT value FROM undo_state WHERE key = 'current_group'").fetchone()[0]
    conn.execute(
        "INSERT INTO undo_groups (id, description, timestamp) VALUES (?, ?, ?)",
        (group_id, description, datetime.now(UTC).isoformat())
    )
    # Clear redo history (any undone groups)
    conn.execute("DELETE FROM undo_log WHERE undo_group IN (SELECT id FROM undo_groups WHERE undone = 1)")
    conn.execute("DELETE FROM undo_groups WHERE undone = 1")
    conn.commit()
    return group_id

def undo_execute(project_dir: Path) -> dict | None:
    """Undo the last operation. Returns group info or None."""
    conn = get_db(project_dir)
    group = conn.execute(
        "SELECT * FROM undo_groups WHERE undone = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not group:
        return None
    
    # Disable triggers
    conn.execute("UPDATE undo_state SET value = 0 WHERE key = 'active'")
    
    # Execute inverse SQL in reverse order
    rows = conn.execute(
        "SELECT sql_text FROM undo_log WHERE undo_group = ? ORDER BY seq DESC",
        (group["id"],)
    ).fetchall()
    for row in rows:
        conn.execute(row["sql_text"])
    
    # Mark as undone (for redo)
    conn.execute("UPDATE undo_groups SET undone = 1 WHERE id = ?", (group["id"],))
    
    # Re-enable triggers
    conn.execute("UPDATE undo_state SET value = 1 WHERE key = 'active'")
    conn.commit()
    
    return {"id": group["id"], "description": group["description"]}

def undo_history(project_dir: Path, limit: int = 50) -> list[dict]:
    """Get recent undo history."""
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT id, description, timestamp, undone FROM undo_groups ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
```

### API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/projects/:name/undo` | Undo last operation |
| POST | `/api/projects/:name/redo` | Redo last undo (P1) |
| GET | `/api/projects/:name/undo-history` | List recent operations |

### CLI Commands

```
.venv/bin/python3 -m beatlab undo           # undo last operation
.venv/bin/python3 -m beatlab redo           # redo last undo
.venv/bin/python3 -m beatlab undo-history   # show recent history
```

### Integration Pattern (API Server)

Each endpoint that modifies the timeline wraps its operation:

```python
group_id = undo_begin(project_dir, f"Update {kf_id} prompt")
try:
    update_keyframe(project_dir, kf_id, prompt=new_prompt)
except:
    # Triggers already captured the changes — if we need to rollback,
    # the undo_log has the inverse. But typically we let it stand.
    raise
```

### Tracked Tables

| Table | INSERT | UPDATE | DELETE |
|---|---|---|---|
| keyframes | ✓ | ✓ | ✓ |
| transitions | ✓ | ✓ | ✓ |
| suppressions | ✓ | ✓ | ✓ |
| effects | ✓ | ✓ | ✓ |

**Excluded**: meta, undo_log, undo_groups, undo_state, settings.yaml

---

## Scope

### What Supports Undo
- All keyframe operations (create, update, soft-delete, split)
- All transition operations (create, update, soft-delete, split, merge)
- Suppression zone changes
- User effect changes
- Batch operations (batch-set-base-image, bulk pool insert)
- Track operations (add/remove/reorder)

### What Does NOT Support Undo
- Video/image generation (assets stay on disk permanently)
- Render operations (outputs stay on disk)
- Settings changes (operational config, not creative state)

---

## Configuration

- **Max undo history**: 1,000 groups (pruned FIFO when exceeded)
- **Redo**: Planned (P1), cleared on new operation (standard behavior)
- **Concurrency**: Global undo stack, single-editor model
- **Persistence**: project.db — survives restarts and sessions

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Approach | Hybrid trigger + command | Triggers catch everything automatically; command layer adds grouping + descriptions |
| Persistence | project.db | Must survive restarts; already the source of truth |
| Max depth | 1,000 groups | ~100 practical undos, 1,000 for audit trail |
| Redo | P1 (planned, not P0) | Standard redo behavior, cleared on new op |
| Asset deletion | Never | Undo restores DB pointers, assets stay on disk |
| Settings | Excluded | Operational config, not creative edits |
| Concurrency | Global stack | Single-editor model, same as rest of app |
| Trigger safety | `undo_state.active` flag | Prevents recursive logging during undo replay |
| Conflict handling | Stack ordering prevents | Must undo in order; can't skip operations |

---

## Files

| File | Changes |
|---|---|
| `src/beatlab/db.py` | Add undo tables, triggers, undo_begin/execute/history functions |
| `src/beatlab/api_server.py` | Wrap modifying endpoints with undo_begin, add undo/redo/history endpoints |
| `src/beatlab/__main__.py` | Add undo/redo/undo-history CLI commands |
