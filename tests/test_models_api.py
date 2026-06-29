from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import openai_router


def _build_app() -> TestClient:
    app = FastAPI()
    app.include_router(openai_router.router)
    return TestClient(app)


def test_openai_models_response_uses_numeric_created(monkeypatch):
    monkeypatch.setattr(
        openai_router,
        "get_available_models_async",
        AsyncMock(return_value=["gpt-5.5", "gemini-3.5-flash"]),
    )

    response = _build_app().get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [item["id"] for item in body["data"]] == ["gpt-5.5", "gemini-3.5-flash"]
    assert all(item["object"] == "model" for item in body["data"])
    assert all(isinstance(item["created"], int) for item in body["data"])
    assert all(item["owned_by"] == "assemblyai" for item in body["data"])
