"""APIRouter modules — one per domain (spec R4).

T57 shipped two:
  - ``misc``  — ``GET /api/config`` smoke route
  - ``files`` — ``GET`` / ``HEAD`` ``/api/projects/{name}/files/{file_path:path}``

T58 added ``auth`` and ``oauth``. T59 added ``test_harness`` (testing-only).

T61 adds:
  - ``keyframes``   — 25 keyframe mutation routes (7 structural)
  - ``transitions`` — 22 transition mutation routes (4 structural, incl. new
    ``batch-delete-transitions``)

T60/T62/T63/T64 will add the remaining routers covering the full 164-route
business surface.
"""
