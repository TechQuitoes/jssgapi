from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


# ---------------------------------------------------------------------------
# Helpers — mock _translate_via_azure so tests never hit the real network
# ---------------------------------------------------------------------------

def _azure_mock(translated_text: str, detected: str = "en"):
    """Return a mock that replaces ``_translate_via_azure`` in translate_service."""
    from app.services.translate_service import TranslationResult

    def _fake(text, target_language, source_language=None, timeout=10.0):
        return TranslationResult(
            original_text=text,
            translated_text=translated_text,
            source_language=source_language or detected,
            target_language=target_language,
            detected_language=detected,
        )

    return _fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_translate_en_to_hi():
    with patch(
        "app.services.translate_service._translate_via_azure",
        side_effect=_azure_mock("गेहूं एक महत्वपूर्ण फसल है।"),
    ), TestClient(app) as client:
        response = client.post(
            "/api/translate",
            json={"text": "Wheat is an important crop.", "target_language": "hi"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["originalText"] == "Wheat is an important crop."
    assert body["translatedText"] == "गेहूं एक महत्वपूर्ण फसल है।"
    assert body["targetLanguage"] == "hi"


def test_translate_hi_to_en():
    with patch(
        "app.services.translate_service._translate_via_azure",
        side_effect=_azure_mock("Wheat is an important crop.", detected="hi"),
    ), TestClient(app) as client:
        response = client.post(
            "/api/translate",
            json={
                "text": "गेहूं एक महत्वपूर्ण फसल है।",
                "target_language": "en",
                "source_language": "hi",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["targetLanguage"] == "en"


def test_translate_empty_input():
    with TestClient(app) as client:
        response = client.post("/api/translate", json={"text": "   ", "target_language": "hi"})
    assert response.status_code == 400


def test_translate_too_long():
    long_text = "a" * 5001
    with TestClient(app) as client:
        response = client.post("/api/translate", json={"text": long_text, "target_language": "hi"})
    assert response.status_code == 422  # pydantic validation catches > max_length


def test_translate_missing_config():
    """When the active provider raises ServiceUnavailableException, the API must return 503."""
    from app.services import translate_service

    def raise_exc(*args, **kwargs):
        raise translate_service.ServiceUnavailableException("Translation service is not configured")

    # Patch the internal dispatcher so it always raises, regardless of which
    # provider is configured.
    with patch(
        "app.services.translate_service._translate_text", side_effect=raise_exc
    ), TestClient(app) as client:
        response = client.post("/api/translate", json={"text": "Hello", "target_language": "hi"})
    assert response.status_code == 503


def test_translate_api_failure():
    """Any ServiceUnavailableException from the provider must surface as 503."""
    from app.services import translate_service

    def raise_exc(*args, **kwargs):
        raise translate_service.ServiceUnavailableException("Translation service failed")

    with patch(
        "app.services.translate_service._translate_text", side_effect=raise_exc
    ), TestClient(app) as client:
        response = client.post("/api/translate", json={"text": "Hello", "target_language": "hi"})
    assert response.status_code == 503


def test_existing_apis_unaffected():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
