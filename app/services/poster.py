from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from uuid import uuid4

import httpx

from app.config import Settings
from app.models.schemas import PosterBrief

_WORKFLOW_TEMPLATE_PATH = Path(__file__).parent / "comfyui" / "workflow_template.json"

ASPECT_RATIO_SIZES: dict[str, tuple[int, int]] = {
    "3:4": (896, 1152),
    "4:3": (1152, 896),
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
}

POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 300.0


class PosterService:
    """Adapter for a ComfyUI text-to-image endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def generate_background(self, brief: PosterBrief) -> dict[str, str]:
        if self.settings.mock_imagegen:
            return {
                "asset_id": f"poster-mock-{uuid4().hex[:8]}",
                "status": "generated",
                "url": "/demo-assets/hangzhou-poster-background.png",
                "note": "Mock asset. Set MOCK_MODEL_MODE=false for real generation.",
            }

        workflow = self._build_workflow(brief)
        base_url = self.settings.imagegen_api_url.rstrip("/")

        async with httpx.AsyncClient(timeout=30) as client:
            queue_response = await client.post(
                f"{base_url}/prompt",
                json={"prompt": workflow},
            )
            queue_response.raise_for_status()
            prompt_id: str = queue_response.json()["prompt_id"]

            image_info = await self._poll_history(client, base_url, prompt_id)

        return {
            "asset_id": f"poster-{uuid4().hex[:8]}",
            "status": "generated",
            "prompt_id": prompt_id,
            "url": image_info["url"],
            "filename": image_info["filename"],
        }

    def _build_workflow(self, brief: PosterBrief) -> dict:
        template = json.loads(_WORKFLOW_TEMPLATE_PATH.read_text(encoding="utf-8"))

        positive_prompt = (
            f"{brief.destination}, {brief.product_theme}, {brief.visual_style}, "
            f"color palette: {', '.join(brief.primary_colors)}, "
            f"elements: {', '.join(brief.visual_elements)}. "
            "Professional travel poster, high quality, detailed illustration, "
            "reserve top and bottom copy space, no text, no watermark."
        )
        negative_prompt = ", ".join(brief.negative_elements) or (
            "text, logo, QR code, watermark, low quality, blurry, deformed"
        )

        width, height = ASPECT_RATIO_SIZES.get(brief.aspect_ratio, (896, 1152))

        template["6"]["inputs"]["text"] = positive_prompt
        template["7"]["inputs"]["text"] = negative_prompt
        template["5"]["inputs"]["width"] = width
        template["5"]["inputs"]["height"] = height
        template["3"]["inputs"]["seed"] = random.randint(0, 2**53)

        return template

    async def _poll_history(
        self, client: httpx.AsyncClient, base_url: str, prompt_id: str
    ) -> dict[str, str]:
        elapsed = 0.0
        while elapsed < POLL_TIMEOUT_SECONDS:
            history_response = await client.get(f"{base_url}/history/{prompt_id}")
            history_response.raise_for_status()
            history = history_response.json()

            if prompt_id in history:
                outputs = history[prompt_id].get("outputs", {})
                for node_output in outputs.values():
                    images = node_output.get("images", [])
                    if images:
                        image = images[0]
                        filename = image["filename"]
                        subfolder = image.get("subfolder", "")
                        image_type = image.get("type", "output")
                        view_url = (
                            f"{base_url}/view?filename={filename}"
                            f"&subfolder={subfolder}&type={image_type}"
                        )
                        return {"url": view_url, "filename": filename}
                raise RuntimeError(
                    f"ComfyUI prompt {prompt_id} completed but no images found in outputs."
                )

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        raise TimeoutError(
            f"ComfyUI prompt {prompt_id} did not complete within {POLL_TIMEOUT_SECONDS}s."
        )
