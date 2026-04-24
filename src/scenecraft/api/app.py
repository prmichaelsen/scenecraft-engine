"""FastAPI application factory (M16 T57).

Module-level ``app`` is required (spec R2) so tests and ``uvicorn
scenecraft.api.app:app`` can both reach the same instance. For tests
that need a custom ``work_dir``, call ``create_app(work_dir=...)``.

Legacy ``api_server.py`` still runs during Phase A — this module
ships on a parallel port (8890) until T65's hard cutover deletes
``api_server.py`` and rewires ``scenecraft.cli`` to start uvicorn.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from scenecraft.api.deps import install_cors
from scenecraft.api.errors import install_exception_handlers
from scenecraft.api.routers import files, misc


def create_app(work_dir: Path | None = None, *, enable_docs: bool = True) -> FastAPI:
    """Build a configured FastAPI app rooted at ``work_dir``.

    ``work_dir`` may be ``None`` when the app is imported for
    OpenAPI introspection or Swagger UI rendering; file routes will
    500 on that configuration, which is the correct failure mode.
    """
    app = FastAPI(
        title="scenecraft-engine",
        description=(
            "FastAPI migration of the legacy api_server.py. "
            "M16 scaffold — file streaming + OpenAPI spike."
        ),
        openapi_url="/openapi.json",
        docs_url="/docs" if enable_docs else None,
        redoc_url="/redoc" if enable_docs else None,
    )
    install_cors(app)
    install_exception_handlers(app)
    app.include_router(misc.router)
    app.include_router(files.router)
    app.state.work_dir = Path(work_dir) if work_dir is not None else None
    return app


# Module-level app for uvicorn. work_dir=None here — real uvicorn
# boots via scenecraft.cli which passes the resolved work_dir into
# ``create_app``. This default instance is used by ``/openapi.json``
# emission during codegen (T66) where no work_dir is needed.
app = create_app()


__all__ = ["app", "create_app"]
