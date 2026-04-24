"""Pydantic request/response models (populated in T60-T64).

Empty in T57 — the scaffold's routes return raw streams or the
untyped ``load_config()`` dict. Routers that land business logic
will add their per-operation ``Body`` / ``Response`` models here.
"""
