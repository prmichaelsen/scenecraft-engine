"""M16 T57: FastAPI scaffold + Range-aware streaming spike.

Tests the 8 named behaviors from the task file and spec local.fastapi-migration:

    - file_get_no_range           (R20, R22)
    - file_get_range_206          (R21)
    - file_get_range_416          (R21)
    - file_get_suffix_range_416   (R21)   ← legacy has no suffix-range support
    - file_head_metadata_only     (R12)
    - file_traversal_rejected     (R22)
    - openapi_valid_3_1           (R29)
    - swagger_ui_renders          (R31)

TDD order: this file was written BEFORE src/scenecraft/api/ existed. Every
test was run red first (ModuleNotFoundError / no-such-route) and the
implementation was driven until the suite went green.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


FIXTURE_SIZE = 100 * 1024  # 100 KiB


@pytest.fixture()
def project_fixture(tmp_path: Path) -> tuple[Path, str, str, bytes]:
    """Lay out a work_dir with one project + one 100 KiB binary file.

    Returns (work_dir, project_name, relative_file_path, file_bytes).
    """
    work_dir = tmp_path / "work"
    project_name = "P1"
    project_dir = work_dir / project_name / "assets"
    project_dir.mkdir(parents=True)

    # Deterministic content so we can assert byte-for-byte equality.
    file_bytes = bytes((i * 7) % 256 for i in range(FIXTURE_SIZE))
    (project_dir / "test.bin").write_bytes(file_bytes)

    # Also plant a sibling project whose file we should NOT be able to reach
    # via a ../ traversal attempt.
    other = work_dir / "other-project"
    other.mkdir(parents=True)
    (other / "secret.txt").write_bytes(b"do not reveal")

    return work_dir, project_name, "assets/test.bin", file_bytes


@pytest.fixture()
def client(project_fixture):
    """Build the FastAPI app rooted at the test work_dir and wrap in TestClient."""
    work_dir, _project_name, _file_path, _file_bytes = project_fixture

    from scenecraft.api.app import create_app

    app = create_app(work_dir=work_dir)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Range-aware file serving
# ---------------------------------------------------------------------------


def test_file_get_no_range(client, project_fixture):
    """file-get-no-range (R20, R22).

    GET without Range returns 200, full bytes, Accept-Ranges: bytes.
    """
    _, project_name, file_path, file_bytes = project_fixture
    url = f"/api/projects/{project_name}/files/{file_path}"

    resp = client.get(url)

    assert resp.status_code == 200
    assert resp.headers.get("accept-ranges") == "bytes"
    assert int(resp.headers["content-length"]) == FIXTURE_SIZE
    assert resp.content == file_bytes


def test_file_get_range_206(client, project_fixture):
    """file-get-range-206 (R21).

    `Range: bytes=0-999` → 206, Content-Range `bytes 0-999/102400`,
    body is exactly 1000 bytes matching file_bytes[0:1000].
    """
    _, project_name, file_path, file_bytes = project_fixture
    url = f"/api/projects/{project_name}/files/{file_path}"

    resp = client.get(url, headers={"Range": "bytes=0-999"})

    assert resp.status_code == 206
    assert resp.headers.get("content-range") == f"bytes 0-999/{FIXTURE_SIZE}"
    assert resp.headers.get("accept-ranges") == "bytes"
    assert int(resp.headers["content-length"]) == 1000
    assert len(resp.content) == 1000
    assert resp.content == file_bytes[0:1000]


def test_file_get_range_416(client, project_fixture):
    """file-get-range-416 (R21).

    Out-of-bounds start → 416 with `Content-Range: bytes */<size>`.
    """
    _, project_name, file_path, _file_bytes = project_fixture
    url = f"/api/projects/{project_name}/files/{file_path}"

    resp = client.get(url, headers={"Range": "bytes=200000-300000"})

    assert resp.status_code == 416
    assert resp.headers.get("content-range") == f"bytes */{FIXTURE_SIZE}"


def test_file_get_suffix_range_416(client, project_fixture):
    """file-get-suffix-range-416 (R21).

    Suffix ranges (`bytes=-N`) are rejected with 416 — legacy parity,
    the stdlib handler never supported `bytes=-100`-style requests.
    """
    _, project_name, file_path, _file_bytes = project_fixture
    url = f"/api/projects/{project_name}/files/{file_path}"

    resp = client.get(url, headers={"Range": "bytes=-100"})

    assert resp.status_code == 416


def test_file_head_metadata_only(client, project_fixture):
    """file-head-metadata-only (R12).

    HEAD returns 200 with Content-Length + Accept-Ranges, empty body.
    """
    _, project_name, file_path, _file_bytes = project_fixture
    url = f"/api/projects/{project_name}/files/{file_path}"

    resp = client.head(url)

    assert resp.status_code == 200
    assert int(resp.headers["content-length"]) == FIXTURE_SIZE
    assert resp.headers.get("accept-ranges") == "bytes"
    # HEAD responses must have empty body.
    assert resp.content == b""


def test_file_traversal_rejected(client, project_fixture):
    """file-traversal-rejected (R22).

    `..` escaping out of work_dir/project_name is rejected with 404 and
    the legacy error envelope `{"error": "NOT_FOUND", "message": "..."}`.
    """
    _, project_name, _file_path, _file_bytes = project_fixture
    url = f"/api/projects/{project_name}/files/../other-project/secret.txt"

    resp = client.get(url)

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "NOT_FOUND"
    assert "message" in body
    # Do NOT leak absolute filesystem paths in the message.
    assert "/work/" not in body["message"]


# ---------------------------------------------------------------------------
# OpenAPI + Swagger UI
# ---------------------------------------------------------------------------


def test_openapi_valid_3_1(client):
    """openapi-valid-3-1 (R29).

    `GET /openapi.json` returns a valid OpenAPI 3.1 document.
    """
    from openapi_spec_validator import validate

    resp = client.get("/openapi.json")
    assert resp.status_code == 200

    spec = resp.json()
    # FastAPI 0.110+ defaults to 3.1.0. Accept any 3.1.x.
    assert spec["openapi"].startswith("3.1"), spec["openapi"]

    # Raises if invalid.
    validate(spec)

    # The two file routes registered in this spike must have the
    # operationIds consumed by the Phase B codegen (T66-T68).
    op_ids = {
        op["operationId"]
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    }
    assert "get_project_file" in op_ids
    assert "head_project_file" in op_ids


def test_swagger_ui_renders(client):
    """swagger-ui-renders (R31).

    `GET /docs` returns 200 HTML containing a `swagger-ui` reference.
    """
    resp = client.get("/docs")
    assert resp.status_code == 200
    body = resp.text
    assert "swagger-ui" in body
