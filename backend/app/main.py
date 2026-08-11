from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.accuracy import router as accuracy_router
from app.api.v1.agents import router as agents_router
from app.api.v1.costs import router as costs_router
from app.api.v1.documents import router as documents_router
from app.api.v1.exports import router as exports_router
from app.api.v1.exposure import router as exposure_router
from app.api.v1.health import router as health_router
from app.api.v1.review import router as review_router
from app.api.v1.runs import router as runs_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.services.pipeline import reap_stale_jobs

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A non-terminal extraction_jobs row can only be left over from a process
    # that died before finishing it -- never a job this fresh process is
    # already tracking, since it just started. See services/pipeline.py's
    # reap_stale_jobs docstring (stub today, real UPDATE with phase B1).
    reap_stale_jobs()
    yield


app = FastAPI(title="Breach Analytics", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(health_router, prefix="/api/v1")
app.include_router(runs_router, prefix="/api/v1")
app.include_router(documents_router, prefix="/api/v1")
app.include_router(exposure_router, prefix="/api/v1")
app.include_router(review_router, prefix="/api/v1")
app.include_router(agents_router, prefix="/api/v1")
app.include_router(costs_router, prefix="/api/v1")
app.include_router(accuracy_router, prefix="/api/v1")
app.include_router(exports_router, prefix="/api/v1")
