"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hiveweave.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown.

    契约 15: SystemState + Application
    - Startup: init Meta DB, run migrations, recover projects, seed default model
    - Shutdown: persist game time, close DB connections
    """
    from hiveweave.db.meta import init_meta_db, close_meta_db
    from hiveweave.db.project import close_all as close_project_dbs

    await init_meta_db()
    yield
    await close_project_dbs()
    await close_meta_db()


app = FastAPI(
    title="HiveWeave API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    """契约 19: Health endpoint."""
    return {"status": "ok", "version": "0.1.0"}


@app.get("/")
async def root():
    """契约 19: Root — HTML landing page (simplified)."""
    return {
        "name": "HiveWeave API",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/api/health",
    }
