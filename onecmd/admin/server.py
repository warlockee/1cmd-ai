"""Admin panel FastAPI application factory and uvicorn launcher.

Calling spec:
  Inputs: ValidatedBackend, Config, Store, ManagerRouter
  Outputs: FastAPI app
  Side effects: starts uvicorn in daemon thread

Factory:
  create_app(backend, config, store, router) -> FastAPI
  start_admin(app, host, port) -> threading.Thread

Static files served from onecmd/admin/static/.
Root "/" returns index.html.
Auth required on all /api/* and /ws/* routes (except /api/auth/*).
"""


import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from onecmd.admin.auth import auth_router, resolve_password

if TYPE_CHECKING:
    from onecmd.config import Config
    from onecmd.manager.router import ManagerRouter
    from onecmd.store import Store
    from onecmd.terminal.backend import ValidatedBackend

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    backend: ValidatedBackend,
    config: Config,
    store: Store,
    router: ManagerRouter,
) -> FastAPI:
    """Create and configure the FastAPI admin application.

    Stores shared resources on ``app.state`` so route handlers can access
    them without global imports.
    """
    app = FastAPI(title="OneCmd Admin", docs_url=None, redoc_url=None)

    # -- Store shared resources on app.state --
    app.state.backend = backend
    app.state.config = config
    app.state.store = store
    app.state.router = router

    # -- Resolve admin password early (logs it if auto-generated) --
    resolve_password(config.admin_password)

    # -- Include routers --
    app.include_router(auth_router)

    from onecmd.admin.routes_terminals import terminals_router
    app.include_router(terminals_router)

    from onecmd.admin.routes_editor import editor_router
    app.include_router(editor_router)

    from onecmd.admin.routes_cron import cron_router
    app.include_router(cron_router)

    # Initialize cron store and engine on app.state
    from onecmd.cron.store import CronStore
    from onecmd.cron.engine import CronEngine
    app.state.cron_store = CronStore()
    app.state.cron_engine = CronEngine(
        store=app.state.cron_store,
        backend=backend,
        config=config,
    )
    app.state.cron_engine.start()

    from onecmd.admin.routes_connectors import connectors_router
    app.include_router(connectors_router)

    from onecmd.admin.routes_webhooks import webhooks_router
    app.include_router(webhooks_router)

    # -- Static files --
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # -- Root route: serve index.html --
    @app.get("/")
    async def index():
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(str(index_path))
        return {"message": "OneCmd Admin — static files not found"}

    # -- Health check (no auth required) --
    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# Uvicorn launcher
# ---------------------------------------------------------------------------


def start_admin(
    app: FastAPI,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> threading.Thread:
    """Run uvicorn in a daemon thread. Returns the thread."""
    import uvicorn

    uvi_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        # Disable access logs to keep console clean (admin is low-traffic)
        access_log=False,
    )
    server = uvicorn.Server(uvi_config)

    thread = threading.Thread(
        target=server.run,
        name="admin-server",
        daemon=True,
    )
    thread.start()

    log.info("Admin server started on http://%s:%d", host, port)
    return thread
