"""Pydantic bodies for the chat router (M16 T64).

``GET /chat?limit=50`` uses FastAPI's native query binding (no model
needed), and the SQL-query endpoint takes a body with ``sql`` + ``limit``
fields that mirror the chat tool's input schema exactly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SqlQueryBody(BaseModel):
    """POST /api/projects/{name}/sql/query body.

    Mirrors the ``sql_query`` chat tool input_schema — ``sql`` is the
    read-only SELECT statement, ``limit`` caps the row count (server
    clamps to 1..100_000 regardless).
    """

    sql: str
    limit: int = Field(default=100, ge=1, le=100_000)


__all__ = ["SqlQueryBody"]
