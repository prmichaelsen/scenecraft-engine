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
from scenecraft.api.routers import (
    audio_clips,
    audio_intelligence,
    audio_tracks,
    auth,
    bench,
    candidates,
    chat,
    checkpoints,
    config as config_router,
    effect_curves,
    effects,
    files,
    ingredients,
    keyframes,
    markers,
    misc,
    mix_render,
    oauth,
    plugins,
    pool,
    projects,
    prompt_roster,
    rendering,
    settings as settings_router,
    transitions,
    workspace,
)


def create_app(
    work_dir: Path | None = None,
    *,
    enable_docs: bool = True,
    testing: bool = False,
) -> FastAPI:
    """Build a configured FastAPI app rooted at ``work_dir``.

    ``work_dir`` may be ``None`` when the app is imported for
    OpenAPI introspection or Swagger UI rendering; file routes will
    500 on that configuration, which is the correct failure mode.

    ``testing=True`` mounts the ``test_harness`` router (debug routes
    used by M16 T59 concurrency tests). It is NEVER set in the
    module-level ``app`` below, so production boots never expose the
    harness endpoints.
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
        redirect_slashes=False,
    )
    install_cors(app)
    install_exception_handlers(app)
    app.include_router(auth.router)
    app.include_router(oauth.router)
    app.include_router(oauth.callback_router)
    app.include_router(misc.router)
    app.include_router(files.router)
    # M16 T60 — projects + misc routers.
    app.include_router(projects.router)
    app.include_router(workspace.router)
    app.include_router(settings_router.router)
    app.include_router(ingredients.router)
    app.include_router(bench.router)
    app.include_router(markers.router)
    app.include_router(prompt_roster.router)
    app.include_router(config_router.router)
    # M16 T61 — keyframes + transitions routers.
    app.include_router(keyframes.router)
    app.include_router(transitions.router)
    # M16 T62 — audio routers.
    app.include_router(audio_tracks.router)
    app.include_router(audio_clips.router)
    app.include_router(effect_curves.router)
    app.include_router(mix_render.router)
    app.include_router(audio_intelligence.router)
    # M16 T63 — rendering, files, pool, candidates.
    app.include_router(rendering.router)
    app.include_router(pool.router)
    app.include_router(candidates.router)
    app.include_router(effects.router)
    # M16 T64 — checkpoints, chat, plugins.
    app.include_router(checkpoints.router)
    app.include_router(chat.router)
    # Plugin catch-all MUST be last (after all built-in routes).
    app.include_router(plugins.router)
    app.state.work_dir = Path(work_dir) if work_dir is not None else None
    app.state.testing = bool(testing)
    if app.state.testing:
        # Imported lazily so production boots never pay the cost or
        # expose the module to module-level side-effects.
        from scenecraft.api.routers import test_harness

        app.include_router(test_harness.router)
    return app


# Module-level app for uvicorn. work_dir=None here — real uvicorn
# boots via scenecraft.cli which passes the resolved work_dir into
# ``create_app``. This default instance is used by ``/openapi.json``
# emission during codegen (T66) where no work_dir is needed.
app = create_app()


__all__ = ["app", "create_app"]
