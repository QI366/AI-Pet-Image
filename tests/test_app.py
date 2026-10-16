import base64

import pytest
from fastapi.testclient import TestClient

import app as service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DB_PATH", tmp_path / "history.db")
    monkeypatch.setattr(service, "HISTORY_DIR", tmp_path / "history_images")
    service.HISTORY_DIR.mkdir()
    service.init_db()
    monkeypatch.delenv("MODEL_OPENAI_API_KEY", raising=False)
    with TestClient(service.app) as test_client:
        yield test_client


def test_demo_routes_work_without_api_key(client):
    assert client.get("/health").json() == {"ok": True, "service": "ai-pet-image"}
    page = client.get("/")
    assert page.status_code == 200
    assert "Pet Portrait Studio" in page.text
    assert client.get("/api/config").json()["default_model"] == service.DEFAULT_MODEL


def test_generation_requires_configured_api_key(client):
    response = client.post(
        "/api/generate",
        files={"file": ("pet.png", b"\x89PNG\r\n\x1a\nimage", "image/png")},
        data={"model": service.DEFAULT_MODEL, "prompt": "portrait"},
    )
    assert response.status_code == 503
    assert "MODEL_OPENAI_API_KEY" in response.json()["detail"]


def test_prompt_version_endpoint_rejects_oversized_content(client):
    response = client.post("/api/prompts", data={"content": "x" * 4001})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("filename", "content", "content_type", "expected_status"),
    [
        ("pet.txt", b"not an image", "text/plain", 415),
        ("pet.png", b"\x89PNG\r\n\x1a\nimage", "image/png", 422),
    ],
)
def test_generation_rejects_invalid_upload_and_unconfigured_model(
    client, monkeypatch, filename, content, content_type, expected_status
):
    monkeypatch.setenv("MODEL_OPENAI_API_KEY", "test-key")
    response = client.post(
        "/api/generate",
        files={"file": (filename, content, content_type)},
        data={"model": "untrusted-model", "prompt": "portrait"},
    )
    assert response.status_code == expected_status


def test_generation_persists_result_and_prompt_version(client, monkeypatch):
    monkeypatch.setenv("MODEL_OPENAI_API_KEY", "test-key")
    image = b"\x89PNG\r\n\x1a\nmock image bytes"
    encoded = base64.b64encode(image).decode()
    calls = []

    def fake_generate(model, image_bytes, filename, prompt):
        calls.append(model)
        return encoded, True, {"input_tokens": 20, "output_tokens": 30, "total_tokens": 50}

    monkeypatch.setattr(service, "_call_image_model", fake_generate)
    response = client.post(
        "/api/generate",
        files={"file": ("pet.png", b"\x89PNG\r\n\x1a\nsource", "image/png")},
        data={"model": service.DEFAULT_MODEL, "prompt": "A portrait of my dog"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["history_saved"] is True
    assert payload["prompt_version_number"] == 2
    assert calls == [service.DEFAULT_MODEL]
    history = client.get("/api/history").json()
    assert history["items"][0]["id"] == payload["history_id"]
    assert history["items"][0]["prompt_text"] == "A portrait of my dog"
    assert client.get(history["items"][0]["image_url"]).content == image


def test_generation_uses_fallback_after_primary_failure(client, monkeypatch):
    monkeypatch.setenv("MODEL_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(service, "FALLBACK_MODEL", "test-fallback")
    monkeypatch.setattr(service, "MODEL_OPTIONS", [service.DEFAULT_MODEL, "test-fallback"])
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nresult").decode()
    calls = []

    def fake_generate(model, image_bytes, filename, prompt):
        calls.append(model)
        if len(calls) == 1:
            raise RuntimeError("provider detail should not reach the API response")
        return encoded, True, None

    monkeypatch.setattr(service, "_call_image_model", fake_generate)
    response = client.post(
        "/api/generate",
        files={"file": ("pet.png", b"\x89PNG\r\n\x1a\nsource", "image/png")},
        data={"model": service.DEFAULT_MODEL, "prompt": "portrait"},
    )
    assert response.status_code == 200
    assert response.json()["is_fallback"] is True
    assert response.json()["used_model"] == "test-fallback"
    assert calls == [service.DEFAULT_MODEL, "test-fallback"]
