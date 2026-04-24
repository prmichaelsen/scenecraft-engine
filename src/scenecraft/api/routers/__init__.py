"""APIRouter modules — one per domain (spec R4).

T57 ships two:
  - ``misc``  — ``GET /api/config`` smoke route
  - ``files`` — ``GET`` / ``HEAD`` ``/api/projects/{name}/files/{file_path:path}``

T60-T64 add the remaining 18 routers covering all 164 business routes.
"""
