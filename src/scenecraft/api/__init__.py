"""FastAPI package for scenecraft-engine (M16).

Parallel to the legacy ``api_server.py``. During Phase A (T57-T64) both
servers coexist; T65's hard cutover deletes ``api_server.py`` and
``scenecraft.cli`` starts this app via ``uvicorn.run``.

This package is intentionally small in T57 — it proves Range-aware file
streaming and ``/openapi.json`` emission before any of the 164 business
routes are ported in T60-T64.
"""
