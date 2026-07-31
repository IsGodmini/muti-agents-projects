from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI):
    from app.services.db import close_pool, get_pool

    await get_pool()
    yield
    await close_pool()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Multi-agent travel product planning and proposal delivery API.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix=settings.api_prefix)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": settings.app_name, "docs": "/docs"}
