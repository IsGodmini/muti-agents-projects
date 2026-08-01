from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TypeVar

import httpx
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
    """Download http(s) images and convert to base64 data URLs.

    Ark 多模态模型会自行抓取 URL，图片链接失效/防盗链时容易返回 400
    （InvalidParameter: Timeout while downloading url）。改为本地下载后
    以 data URL 发送，由调用方控制下载与大小。

    单个图片下载失败/超时/过大时跳过该图并记录日志；全部失败时返回空列表，
    调用方可根据需要降级为纯文本请求（LLM API 本身仍会正常调用）。
    """
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
                response.raise_for_status()
            content = response.content
            if not content or len(content) > max_bytes:
                logger.warning(
                    "Skip image %s: empty or too large (%d bytes)", url, len(content)
                )
                return None
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                content_type = "image/jpeg"
            encoded = base64.b64encode(content).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
        except Exception as exc:  # noqa: BLE001 - 单个图片失败不阻断整体
            logger.warning("Skip image %s: %s: %s", url, type(exc).__name__, str(exc)[:120])
            return None

    results = await asyncio.gather(*(fetch_one(u) for u in targets))
    data_urls = [r for r in results if r]
    if len(data_urls) < len(targets):
        logger.warning("图片下载成功 %d/%d", len(data_urls), len(targets))
    return data_urls


def _schema_hint(schema: type[BaseModel]) -> str:
    """Build a concise field description instead of dumping full JSON Schema."""
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
            hint += f"  [默认: {default}]"
        lines.append(hint)
    return "\n".join(lines)


class ModelGateway:
    """OpenAI-compatible gateway for LLM services (Ark, Ollama, etc.)."""

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
    ) -> SchemaT:
        if self.settings.mock_model_mode:
            raise RuntimeError("Mock mode expects deterministic graph nodes, not model calls.")

        hint = _schema_hint(schema)
        full_system = (
            f"{system_prompt}\n\n"
            f"请输出一个 JSON 对象，字段如下：\n{hint}\n\n"
            "重要：直接输出 JSON，不要输出思考过程、解释、markdown 或其他文字。"
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
            "thinking": {"type": "disabled"},
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                f"{self.settings.llm_base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]
        content = _extract_json(content)
        return schema.model_validate_json(content)


def _extract_json(text: str) -> str:
    """Return the first complete JSON value embedded in the model response.

    Strips markdown fences and any leading/trailing non-JSON text.
    """
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
