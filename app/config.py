from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]


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

    embedding_base_url: str = "http://10.29.248.167:11434/v1"
    embedding_model: str = "qwen3-embedding:0.6b"

    imagegen_api_url: str = "http://10.29.248.167:8188"
    mock_imagegen: bool = False

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

    database_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
