from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import get_settings
from app.services.plan_store import DATA_DIR

settings = get_settings()

FRONTEND_INDEX = Path(__file__).resolve().parent / "static" / "index.html"

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Multi-agent travel product planning and proposal delivery API.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix=settings.api_prefix)

# 交付物文件（报告 / PDF / 海报）静态访问
app.mount("/files", StaticFiles(directory=DATA_DIR), name="files")


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    """返回 Web 前端页面。"""
    return FileResponse(FRONTEND_INDEX)
