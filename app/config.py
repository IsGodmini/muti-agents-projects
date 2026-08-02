import logging
import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]


class _RedactHTTPSecrets(logging.Filter):
    """Prevent API credentials in query strings from reaching INFO logs."""

    _pattern = re.compile(r"([?&](?:key|api_key|token)=)[^&\s]+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        def redact(value: object) -> object:
            if isinstance(value, str) or value.__class__.__module__.startswith("httpx"):
                return self._pattern.sub(r"\1***", str(value))
            return value

        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) for key, value in record.args.items()}
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(item, _RedactHTTPSecrets) for item in _httpx_logger.filters):
    _httpx_logger.addFilter(_RedactHTTPSecrets())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_DIRECTORY / ".env",
        extra="ignore",
    )

    app_name: str = "TripOps AI API"
    environment: str = "development"
    api_prefix: str = "/api/v1"

    llm_base_url: str = "https://ark.cn-beijing.volces.com/api/coding/v3"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "ark-code-latest"

    llm_model_complex: str = "DeepSeek-V4-Pro"
    llm_model_simple: str = "Doubao-Seed-2.0-lite"
    llm_model_multimodal: str = "Doubao-Seed-2.1-turbo"
    llm_max_attempts: int = Field(default=2, ge=1, le=5)

    imagegen_api_url: str = "http://10.29.248.167:8188"
    mock_imagegen: bool = True
    imagegen_queue_timeout_seconds: float = Field(default=900, ge=30, le=3600)
    imagegen_execution_timeout_seconds: float = Field(default=1200, ge=30, le=3600)
    imagegen_max_attempts: int = Field(default=2, ge=1, le=3)

    mock_model_mode: bool = False

    tavily_search_enabled: bool = True
    tavily_mcp_url: str = "https://mcp.tavily.com/mcp"
    tavily_api_key: SecretStr | None = None
    tavily_search_depth: str = "advanced"
    tavily_search_timeout_seconds: float = Field(default=30, ge=5, le=120)

    amap_api_key: str = ""
    amap_base_url: str = "https://restapi.amap.com/v3"

    weather_api_key: str = ""
    weather_base_url: str = "https://devapi.qweather.com/v7"
    weather_geo_base_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
