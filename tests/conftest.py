import pytest


@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_MODEL_MODE", "true")
    monkeypatch.setenv("MOCK_IMAGEGEN", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
