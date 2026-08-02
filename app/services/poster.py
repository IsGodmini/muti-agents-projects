from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from uuid import uuid4

import httpx

from app.config import Settings
from app.models.schemas import PosterBrief

logger = logging.getLogger(__name__)

_WORKFLOW_TEMPLATE_PATH = Path(__file__).parent / "comfyui" / "workflow_template.json"
DATA_DIR = Path(__file__).resolve().parents[2] / "data"

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

        result = {
            "asset_id": f"poster-{uuid4().hex[:8]}",
            "status": "generated",
            "prompt_id": prompt_id,
            "url": image_info["url"],
            "filename": image_info["filename"],
        }

        return result

    async def download_image(self, image_url: str, plan_id: str, name: str = "poster") -> str:
        """Download generated image and save it to the plan directory."""
        plan_dir = DATA_DIR / "plans" / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(image_url.split("?")[0]).suffix or ".png"
        local_path = plan_dir / f"{name}{suffix}"

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(image_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "image/" not in content_type and not response.content.startswith(
                (b"\x89PNG", b"\xff\xd8\xff")
            ):
                raise RuntimeError(f"Downloaded fallback is not an image ({content_type or 'unknown'})")
            local_path.write_bytes(response.content)

        logger.info("Poster saved → %s (%d KB)", local_path, len(response.content) // 1024)
        return str(local_path)

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
        queued_at = time.monotonic()
        execution_started_at: float | None = None
        queue_timeout = getattr(
            self.settings, "imagegen_queue_timeout_seconds", POLL_TIMEOUT_SECONDS
        )
        execution_timeout = getattr(
            self.settings, "imagegen_execution_timeout_seconds", POLL_TIMEOUT_SECONDS
        )
        while True:
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

            queue_response = await client.get(f"{base_url}/queue")
            queue_response.raise_for_status()
            queue = queue_response.json()
            running_ids = {item[1] for item in queue.get("queue_running", [])}
            pending_ids = {item[1] for item in queue.get("queue_pending", [])}
            now = time.monotonic()
            if prompt_id in running_ids:
                execution_started_at = execution_started_at or now
                if now - execution_started_at > execution_timeout:
                    raise TimeoutError(
                        f"ComfyUI prompt {prompt_id} execution exceeded "
                        f"{execution_timeout:.0f}s."
                    )
            elif prompt_id in pending_ids:
                if now - queued_at > queue_timeout:
                    raise TimeoutError(
                        f"ComfyUI prompt {prompt_id} queue wait exceeded {queue_timeout:.0f}s."
                    )
            elif now - queued_at > queue_timeout:
                raise RuntimeError(f"ComfyUI prompt {prompt_id} disappeared from queue and history.")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
