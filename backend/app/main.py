import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api import audit, auth, backups, companies, connectors, documents, vouchers
from app.api import settings as settings_api
from app.config import get_settings
from app.pipeline.worker import worker

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",  # no other site may show these pages in a frame
    "Referrer-Policy": "same-origin",
}


def configure_logging() -> None:
    """Console plus a rotating file (5 x 5 MB) in log_dir, so problems can be looked into
    afterwards. Idempotent: the file handler is added once per process."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    log_dir = get_settings().log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    target = str((log_dir / "app.log").resolve())
    if not any(getattr(h, "baseFilename", None) == target for h in root.handlers):
        handler = RotatingFileHandler(target, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    run_worker = get_settings().run_worker_in_process
    if run_worker:
        worker.start()
    yield
    if run_worker:
        worker.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # Invoices, ledgers and settings must not linger in browser or proxy caches; files
        # that are safe to cache (page images) set their own Cache-Control.
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    for module in (auth, companies, connectors, documents, vouchers, audit, settings_api, backups):
        app.include_router(module.router)

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
