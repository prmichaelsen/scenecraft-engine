"""Integration tests for pool endpoints.

Exercises the endpoint handlers via a live HTTP server running on a random port.
The server auto-provisions the new schema, so these tests cover the end-to-end
shape: request body → handler → DB writes → response.
"""

import json
from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from scenecraft.api.app import create_app
from scenecraft.db import add_pool_segment, add_transition


@pytest.fixture
def server(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    app = create_app(work_dir=work_dir)
    client = TestClient(app, raise_server_exceptions=False)

    yield {"work_dir": work_dir, "client": client}


def _post(srv, path: str, body: dict) -> tuple[int, dict]:
    resp = srv["client"].post(path, json=body)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"error": resp.text}


def _get(srv, path: str) -> tuple[int, dict]:
    resp = srv["client"].get(path)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"error": resp.text}


def _make_project(work_dir: Path, name: str = "myproj") -> Path:
    """Create a project directory and pre-initialize the DB schema.

    Releases the main-thread connection immediately so the server thread's writes
    don't contend on the SQLite lock (each thread maintains its own connection
    via the module-level pool).
    """
    p = work_dir / name
    p.mkdir()
    from scenecraft.db import get_db, close_db
    get_db(p)
    close_db(p)
    return p


def _make_fake_video(path: Path, bytes_: int = 1024) -> Path:
    """Create a dummy file — won't be a real video, but ffprobe failures are tolerated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * bytes_)
    return path


# ── /pool/import ───────────────────────────────────────────────────

def test_import_creates_pool_segments_row(server):
    project = _make_project(server["work_dir"], "imp")
    src = server["work_dir"] / "external_drone.mov"
    _make_fake_video(src, 4096)

    status, resp = _post(server, "/api/projects/imp/pool/import", {
        "sourcePath": str(src),
        "label": "opening drone",
    })
    assert status == 200, resp
    assert resp["success"]
    seg_id = resp["poolSegmentId"]
    assert len(seg_id) == 32
    assert resp["poolPath"].startswith("pool/segments/import_")
    assert resp["poolPath"].endswith(".mov")
    assert resp["originalFilename"] == "external_drone.mov"
    assert resp["originalFilepath"] == str(src)

    # File was copied under the UUID name
    copied = project / resp["poolPath"]
    assert copied.exists()
    assert copied.read_bytes() == src.read_bytes()


def test_upload_multipart_creates_pool_segment(server):
    """Browser-style multipart upload lands as a pool_segments row (kind='imported')."""
    project = _make_project(server["work_dir"], "upl")

    import io
    file_bytes = b"\x00" * 2048
    r = server["client"].post(
        "/api/projects/upl/pool/upload",
        files={"file": ("my_clip.mp4", io.BytesIO(file_bytes), "video/mp4")},
        data={
            "label": "opening shot",
            "originalFilepath": "/home/user/Footage/my_clip.mp4",
        },
    )
    resp = r.json()

    assert resp["success"] is True
    seg_id = resp["poolSegmentId"]
    assert len(seg_id) == 32
    assert resp["poolPath"].startswith("pool/segments/import_")
    assert resp["poolPath"].endswith(".mp4")
    assert resp["originalFilename"] == "my_clip.mp4"
    assert resp["originalFilepath"] == "/home/user/Footage/my_clip.mp4"

    # File landed on disk under the UUID name
    dest = project / resp["poolPath"]
    assert dest.exists()
    assert dest.stat().st_size == 2048

    # DB row surfaces in /pool listing with the right metadata
    _, listing = _get(server, "/api/projects/upl/pool")
    seg = next(s for s in listing["segments"] if s["id"] == seg_id)
    assert seg["kind"] == "imported"
    assert seg["label"] == "opening shot"
    assert seg["originalFilename"] == "my_clip.mp4"
    assert seg["originalFilepath"] == "/home/user/Footage/my_clip.mp4"


def test_import_missing_source(server):
    _make_project(server["work_dir"], "imp")
    status, resp = _post(server, "/api/projects/imp/pool/import", {
        "sourcePath": "/nonexistent/path.mp4",
    })
    assert status == 404


# ── /pool (GET) reads from pool_segments ──────────────────────────

def test_get_pool_returns_segments_with_metadata(server):
    project = _make_project(server["work_dir"], "lst")
    # Seed two segments with realistic metadata
    _make_fake_video(project / "pool/segments/cand_abc.mp4")
    _make_fake_video(project / "pool/segments/import_def.mov")
    g = add_pool_segment(project, kind="generated", created_by="alice",
                        pool_path="pool/segments/cand_abc.mp4", label="",
                        generation_params={"provider": "veo", "prompt": "sunset"})
    i = add_pool_segment(project, kind="imported", created_by="bob",
                        pool_path="pool/segments/import_def.mov",
                        original_filename="drone.mov",
                        original_filepath="/src/drone.mov",
                        label="drone.mov")

    status, resp = _get(server, "/api/projects/lst/pool")
    assert status == 200
    segs = resp["segments"]
    by_id = {s["id"]: s for s in segs}
    assert g in by_id and i in by_id
    assert by_id[g]["kind"] == "generated"
    assert by_id[g]["createdBy"] == "alice"
    assert by_id[g]["generationParams"]["prompt"] == "sunset"
    assert by_id[i]["kind"] == "imported"
    assert by_id[i]["originalFilename"] == "drone.mov"
    assert by_id[i]["originalFilepath"] == "/src/drone.mov"


def test_get_pool_filter_by_kind(server):
    project = _make_project(server["work_dir"], "flt")
    g = add_pool_segment(project, kind="generated", created_by="a",
                        pool_path="pool/segments/cand_1.mp4")
    i = add_pool_segment(project, kind="imported", created_by="a",
                        pool_path="pool/segments/import_1.mp4",
                        original_filename="x.mp4")

    status, resp = _get(server, "/api/projects/flt/pool?kind=generated")
    ids = {s["id"] for s in resp["segments"]}
    assert ids == {g}

    status, resp = _get(server, "/api/projects/flt/pool?kind=imported")
    ids = {s["id"] for s in resp["segments"]}
    assert ids == {i}


# ── /pool/rename ───────────────────────────────────────────────────

def test_rename_updates_label_preserves_attribution(server):
    project = _make_project(server["work_dir"], "ren")
    seg = add_pool_segment(project, kind="imported", created_by="alice",
                          pool_path="pool/segments/import_x.mov",
                          original_filename="raw.mov",
                          label="raw.mov")

    status, resp = _post(server, "/api/projects/ren/pool/rename", {
        "poolSegmentId": seg, "label": "opening drone shot",
    })
    assert status == 200 and resp["success"]

    status, resp = _get(server, "/api/projects/ren/pool")
    seg_row = next(s for s in resp["segments"] if s["id"] == seg)
    assert seg_row["label"] == "opening drone shot"
    # Attribution and original filename are not touched
    assert seg_row["createdBy"] == "alice"
    assert seg_row["originalFilename"] == "raw.mov"


def test_rename_missing_segment_404(server):
    _make_project(server["work_dir"], "ren2")
    status, resp = _post(server, "/api/projects/ren2/pool/rename", {
        "poolSegmentId": "bogus", "label": "whatever",
    })
    assert status == 404


# ── /pool/tag and /pool/untag ──────────────────────────────────────

def test_tag_and_untag(server):
    project = _make_project(server["work_dir"], "tag")
    seg = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/cand_1.mp4")

    status, _ = _post(server, "/api/projects/tag/pool/tag", {
        "poolSegmentId": seg, "tag": "keeper",
    })
    assert status == 200
    status, _ = _post(server, "/api/projects/tag/pool/tag", {
        "poolSegmentId": seg, "tag": "sunset",
    })
    assert status == 200

    status, resp = _get(server, "/api/projects/tag/pool?tag=keeper")
    assert {s["id"] for s in resp["segments"]} == {seg}
    status, resp = _get(server, "/api/projects/tag/pool?tag=sunset")
    assert {s["id"] for s in resp["segments"]} == {seg}
    status, resp = _get(server, "/api/projects/tag/pool?tag=nonexistent")
    assert resp["segments"] == []

    # Untag one
    status, _ = _post(server, "/api/projects/tag/pool/untag", {
        "poolSegmentId": seg, "tag": "sunset",
    })
    assert status == 200
    status, resp = _get(server, "/api/projects/tag/pool?tag=sunset")
    assert resp["segments"] == []
    status, resp = _get(server, "/api/projects/tag/pool?tag=keeper")
    assert {s["id"] for s in resp["segments"]} == {seg}


def test_list_all_tags_with_counts(server):
    project = _make_project(server["work_dir"], "tags")
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                         pool_path="pool/segments/cand_1.mp4")
    s2 = add_pool_segment(project, kind="generated", created_by="a",
                         pool_path="pool/segments/cand_2.mp4")

    for seg_id, tags in [(s1, ["keeper", "sunset"]), (s2, ["keeper"])]:
        for t in tags:
            _post(server, "/api/projects/tags/pool/tag", {
                "poolSegmentId": seg_id, "tag": t,
            })

    status, resp = _get(server, "/api/projects/tags/pool/tags")
    by_name = {t["tag"]: t["count"] for t in resp["tags"]}
    assert by_name["keeper"] == 2
    assert by_name["sunset"] == 1


# ── /pool/gc and /pool/gc-preview ──────────────────────────────────

def test_gc_preview_shows_only_orphaned_generated(server):
    project = _make_project(server["work_dir"], "gc")

    # Orphan generated (no junction row — should appear in preview)
    s_orphan = add_pool_segment(project, kind="generated", created_by="a",
                               pool_path="pool/segments/cand_orphan.mp4")
    _make_fake_video(project / "pool/segments/cand_orphan.mp4", 2048)

    # Used generated (has junction row — should NOT appear)
    s_used = add_pool_segment(project, kind="generated", created_by="a",
                             pool_path="pool/segments/cand_used.mp4")
    add_transition(project, {
        "id": "tr_x", "from": "kf_a", "to": "kf_b",
        "duration_seconds": 4.0, "slots": 1, "action": "", "use_global_prompt": 1,
        "selected": [None], "remap": {"method": "linear", "target_duration": 4.0},
    })
    from scenecraft.db import add_tr_candidate
    add_tr_candidate(project, transition_id="tr_x", slot=0,
                     pool_segment_id=s_used, source="generated")

    # Orphan imported (user asset — should NOT appear)
    s_imp_orphan = add_pool_segment(project, kind="imported", created_by="a",
                                   pool_path="pool/segments/import_stays.mp4",
                                   original_filename="asset.mov")

    status, resp = _get(server, "/api/projects/gc/pool/gc-preview")
    ids = {s["id"] for s in resp["segments"]}
    assert ids == {s_orphan}
    assert resp["wouldDelete"] == 1


def test_gc_deletes_files_and_rows(server):
    project = _make_project(server["work_dir"], "gcd")
    s_orphan = add_pool_segment(project, kind="generated", created_by="a",
                               pool_path="pool/segments/cand_orphan.mp4")
    disk = project / "pool/segments/cand_orphan.mp4"
    _make_fake_video(disk, 4096)

    status, resp = _post(server, "/api/projects/gcd/pool/gc", {})
    assert status == 200
    assert resp["deleted"] == 1
    assert resp["freedBytes"] == 4096
    assert not disk.exists()

    # DB row gone
    status, resp = _get(server, "/api/projects/gcd/pool")
    assert s_orphan not in {s["id"] for s in resp["segments"]}
