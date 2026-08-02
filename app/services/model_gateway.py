from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from typing import TypeVar

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from app.config import Settings

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


async def fetch_images_as_data_urls(
    urls: list[str],
    *,
    max_images: int = 8,
    per_image_timeout: float = 8.0,
    max_bytes: int = 3_000_000,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    targets = [u for u in urls if str(u).startswith(("http://", "https://"))][:max_images]
    if not targets:
        return []

    async def fetch_one(url: str) -> str | None:
        try:
            async with httpx.AsyncClient(
                timeout=per_image_timeout,
                follow_redirects=True,
                transport=transport,
            ) as client:
                response = await client.get(url)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    body = response.text[:500]
                    raise RuntimeError(
                        f"Image download HTTP {response.status_code}: {body}"
                    ) from exc
            content = response.content
            if not content or len(content) > max_bytes:
                logger.warning(
                    "Skip image %s: empty or too large (%d bytes)", url, len(content)
                )
                return None
            # detect image format via magic bytes
            content_type = _detect_image_mime(content[:16])
            if not content_type:
                logger.warning(
                    "Skip image %s: unknown image format (magic=%s)",
                    url, content[:8].hex(),
                )
                return None
            # Ark requires min 14px for both dimensions
            try:
                with Image.open(io.BytesIO(content)) as img:
                    w, h = img.size
                if w < 14 or h < 14:
                    logger.warning(
                        "Skip image %s: too small (%dx%d, min 14)", url, w, h
                    )
                    return None
            except (OSError, UnidentifiedImageError):
                logger.warning("Skip image %s: cannot parse dimensions", url)
                return None
            encoded = base64.b64encode(content).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
        except (httpx.HTTPError, RuntimeError, OSError) as exc:
            logger.warning("Skip image %s: %s: %s", url, type(exc).__name__, str(exc)[:120])
            return None

    results = await asyncio.gather(*(fetch_one(u) for u in targets))
    data_urls = [r for r in results if r]
    if len(data_urls) < len(targets):
        logger.warning("\u56fe\u7247\u4e0b\u8f7d\u6210\u529f %d/%d", len(data_urls), len(targets))
    return data_urls


def _detect_image_mime(head: bytes) -> str | None:
    if len(head) < 4:
        return None
    # PNG
    if head[:4] == b'\x89PNG':
        return "image/png"
    # JPEG
    if head[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    # GIF
    if head[:4] in (b'GIF8',):
        return "image/gif"
    # WebP
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return "image/webp"
    # BMP
    if head[:2] == b'BM':
        return "image/bmp"
    return None


def _schema_hint(schema: type[BaseModel]) -> str:
    props = schema.model_json_schema().get("properties", {})
    lines = []
    for name, info in props.items():
        field_type = info.get("type", "any")
        desc = info.get("description", "")
        default = info.get("default")
        hint = f"  {name}: {field_type}"
        if desc:
            hint += f"  ({desc})"
        if default is not None:
            hint += f"  [\u9ed8\u8ba4: {default}]"
        lines.append(hint)
    return "\n".join(lines)


class ModelGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def structured_completion(
        self,
        *,
        model: str | None = None,
        system_prompt: str,
        user_prompt: str,
        schema: type[SchemaT],
        temperature: float = 0.2,
        timeout_seconds: float = 60,
        image_urls: list[str] | None = None,
        max_attempts: int | None = None,
    ) -> SchemaT:
        if self.settings.mock_model_mode:
            raise RuntimeError("Mock mode expects deterministic graph nodes, not model calls.")

        hint = _schema_hint(schema)
        full_system = (
            f"{system_prompt}\n\n"
            f"\u8bf7\u8f93\u51fa\u4e00\u4e2a JSON \u5bf9\u8c61\uff0c\u5b57\u6bb5\u5982\u4e0b\uff1a\n{hint}\n\n"
            "\u91cd\u8981\uff1a\u76f4\u63a5\u8f93\u51fa JSON\uff0c\u4e0d\u8981\u8f93\u51fa\u601d\u8003\u8fc7\u7a0b\u3001\u89e3\u91ca\u3001markdown \u6216\u5176\u4ed6\u6587\u5b57\u3002"
        )

        api_key = self.settings.llm_api_key.get_secret_value()

        user_content: str | list[dict] = user_prompt
        if image_urls:
            user_content = [{"type": "text", "text": user_prompt}]
            for url in image_urls[:10]:
                user_content.append({"type": "image_url", "image_url": {"url": url}})

        payload: dict = {
            "model": model or self.settings.llm_model,
            "messages": [
                {"role": "system", "content": full_system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        attempt_limit = max_attempts or self.settings.llm_max_attempts
        last_error: Exception | None = None
        for attempt in range(1, attempt_limit + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
                    response = await client.post(
                        f"{self.settings.llm_base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    body = response.text[:500]
                    raise httpx.HTTPStatusError(
                        f"retryable LLM API {response.status_code}: {body}",
                        request=response.request,
                        response=response,
                    )
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    body = response.text[:500]
                    raise RuntimeError(f"LLM API {response.status_code}: {body}") from exc

                content = response.json()["choices"][0]["message"]["content"]
                return schema.model_validate_json(_extract_json(content))
            except RuntimeError:
                raise
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                if attempt >= attempt_limit:
                    break
                delay = min(4.0, 1.5 * attempt)
                logger.warning(
                    "LLM attempt %d/%d failed for %s: %s; retrying in %.1fs",
                    attempt,
                    attempt_limit,
                    model or self.settings.llm_model,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error


def _extract_json(text: str) -> str:
    text = text.strip()
    if "```" in text:
        for part in text.split("```"):
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith(("{", "[")):
                text = candidate
                break
    text = text.strip()

    def _first_json_value(raw: str) -> str | None:
        try:
            decoded, end = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            return None
        return raw[:end] if isinstance(decoded, (dict, list)) else None

    exact = _first_json_value(text)
    if exact is not None:
        return exact
    start = min(filter(lambda i: i != -1, (text.find("{"), text.find("["))), default=-1)
    if start == -1:
        raise ValueError(f"No JSON found in model response: {text[:200]}")
    from_start = _first_json_value(text[start:])
    if from_start is not None:
        return from_start
    return text[start:]
