"""Integration tests for ComfyUI text-to-image service.

These tests require a running ComfyUI instance at IMAGEGEN_API_URL.
Run with: uv run pytest tests/test_comfyui.py -v -s
Skip by default in CI (no ComfyUI available).
"""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.models.schemas import PosterBrief
from app.services.poster import PosterService


def _comfyui_settings() -> Settings:
    return Settings(
        mock_imagegen=False,
        imagegen_api_url="http://10.29.248.167:8188",
    )


@pytest.fixture
def poster_service() -> PosterService:
    return PosterService(_comfyui_settings())


@pytest.mark.integration
class TestComfyUIConnectivity:
    """Verify ComfyUI server is reachable and responsive."""

    async def test_system_stats(self) -> None:
        settings = _comfyui_settings()
        base_url = settings.imagegen_api_url.rstrip("/")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{base_url}/system_stats")
            response.raise_for_status()
            stats = response.json()
            assert "system" in stats or "devices" in stats

    async def test_object_info(self) -> None:
        settings = _comfyui_settings()
        base_url = settings.imagegen_api_url.rstrip("/")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{base_url}/object_info")
            response.raise_for_status()
            nodes = response.json()
            assert "CheckpointLoaderSimple" in nodes
            assert "KSampler" in nodes


@pytest.mark.integration
class TestComfyUIGeneration:
    """End-to-end image generation via ComfyUI."""

    async def test_generate_background(self, poster_service: PosterService) -> None:
        brief = PosterBrief(
            destination="杭州西湖",
            target_audience="亲子家庭",
            product_theme="夏日清凉游",
            visual_style="水彩插画风格",
            primary_colors=["#4A90D9", "#7ED6A5", "#FFF8E7"],
            visual_elements=["西湖断桥", "荷花", "远山"],
            negative_elements=["文字", "水印", "低质量"],
            aspect_ratio="3:4",
        )
        result = await poster_service.generate_background(brief)
        assert result["status"] == "generated"
        assert "url" in result
        assert "prompt_id" in result

        async with httpx.AsyncClient(timeout=10) as client:
            img_response = await client.get(result["url"])
            img_response.raise_for_status()
            assert img_response.headers["content-type"].startswith("image/")
            assert len(img_response.content) > 10_000
