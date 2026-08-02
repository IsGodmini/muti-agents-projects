"""ModelGateway 图片下载（base64 data URL）单元测试。"""
from __future__ import annotations

import base64

import httpx
import pytest

from app.services.model_gateway import fetch_images_as_data_urls


def _png() -> bytes:
    # 1x1 红色 PNG
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAADIAAAAyCAIAAACRXR/mAAAAZklEQVR4nM3OMQEAMAyAMIZ/z52B/iUK8oYiSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkSZIkrwO7D0GqAWPcq78HAAAAAElFTkSuQmCC"
    )


@pytest.mark.asyncio
async def test_ok_and_failed_images_skipped() -> None:
    good = _png()

    def handler(request: httpx.Request) -> httpx.Response:
        if "good" in request.url.host:
            return httpx.Response(200, headers={"content-type": "image/png"}, content=good)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    result = await fetch_images_as_data_urls(
        ["https://good.example/a.png", "https://bad.example/b.png"],
        transport=transport,
    )

    assert len(result) == 1
    assert result[0].startswith("data:image/png;base64,")
    assert base64.b64decode(result[0].split(",", 1)[1]) == good


@pytest.mark.asyncio
async def test_all_failed_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    result = await fetch_images_as_data_urls(
        ["https://bad.example/a.png", "https://bad.example/b.png"],
        transport=transport,
    )
    assert result == []


@pytest.mark.asyncio
async def test_non_http_and_oversize_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_png())

    transport = httpx.MockTransport(handler)
    result = await fetch_images_as_data_urls(
        ["ftp://x/a.png", "https://good.example/a.png"],
        transport=transport,
        max_bytes=4,
    )
    assert result == []
