"""Chat + sql-query router (M16 T64).

Two endpoints:

  * ``GET  /api/projects/{name}/chat`` → ``get_chat_history`` —
    returns the last N messages from ``chat_messages``. Not
    structural — read-only.
  * ``POST /api/projects/{name}/sql/query`` → ``sql_query`` —
    thin wrapper around ``scenecraft.chat._execute_readonly_sql``.
    Gives the ``sql_query`` chat tool a REST peer so T67's
    tool-alignment audit can annotate both sides from one
    OpenAPI spec.

No ``project_lock`` on either — both are read paths.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.models.chat import SqlQueryBody

router = APIRouter(
    prefix="/api/projects",
    tags=["chat"],
    dependencies=[Depends(current_user)],
)


@router.get(
    "/{name}/chat",
    operation_id="get_chat_history",
    summary="Return the tail of the per-user chat history",
)
async def get_chat_history(
    name: str,
    limit: int = Query(50, ge=1, le=1000),
    pd: Path = Depends(project_dir),
) -> dict:
    """Mirror ``api_server.py::GET /chat``.

    The legacy server hardcoded ``user_id="local"`` because chat is
    currently single-user; we keep that until the FE grows multi-user
    history UI (out of scope for M16).
    """
    from scenecraft.chat import _get_messages

    messages = _get_messages(pd, "local", limit)
    return {"messages": messages}


@router.post(
    "/{name}/sql/query",
    operation_id="sql_query",
    summary="Run a read-only SQL SELECT against the project database",
)
async def sql_query(
    name: str,
    body: SqlQueryBody,
    pd: Path = Depends(project_dir),
) -> dict:
    """Thin wrapper over ``chat._execute_readonly_sql``.

    The helper returns a dict that either describes a successful query
    (``columns``/``rows``/``row_count``/``truncated``/``limit``) OR
    carries an ``error`` key when the authorizer denies the statement.
    We forward the dict as-is — legacy parity with the chat tool's
    tool_result payload shape.
    """
    from scenecraft.chat import _execute_readonly_sql

    return _execute_readonly_sql(pd, body.sql, body.limit)


__all__ = ["router"]
