"""Pydantic request/response models for the FastAPI app.

Each router keeps its models colocated in a sibling module
(``models/<router>.py``) so the growing surface doesn't overwhelm
this package's ``__init__``. Routers import via::

    from scenecraft.api.models.checkpoints import CheckpointCreateBody

rather than from the package root — one module per router mirrors
the ``routers/`` layout exactly.
"""
