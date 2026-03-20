import os
from unittest.mock import MagicMock, patch

import pytest

# Must set TESTING before importing app
os.environ["TESTING"] = "1"


@pytest.fixture
def client():
    """Create Flask test client with telegram/webhook bits mocked out."""
    with patch("app.setup_webhook"), patch("app.get_telegram_app"):
        # Re-import to get a fresh app with mocked setup
        import importlib

        import app as app_module

        importlib.reload(app_module)
        app_module.app.config["TESTING"] = True
        with app_module.app.test_client() as c:
            yield c


class TestHealthCheck:
    def test_health_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"

    def test_health_contains_bot_name(self, client):
        resp = client.get("/")
        data = resp.get_json()
        assert "PRE" in data["bot"]


class TestChatPage:
    def test_chat_page_returns_200(self, client):
        resp = client.get("/chat")
        assert resp.status_code == 200
        assert b"PRE Running Coach" in resp.data


class TestApiChat:
    def test_empty_message_returns_400(self, client):
        resp = client.post("/api/chat", json={"message": ""})
        assert resp.status_code == 400

    @patch("app.companion_chat", return_value="Keep running!")
    def test_valid_message_returns_reply(self, mock_chat, client):
        resp = client.post("/api/chat", json={"message": "How far today?"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["reply"] == "Keep running!"
        mock_chat.assert_called_once_with("How far today?", user_id="web_test")

    @patch("app.companion_chat", return_value="Noted!")
    def test_custom_user_id(self, mock_chat, client):
        resp = client.post("/api/chat", json={"message": "hi", "user_id": "custom_123"})
        assert resp.status_code == 200
        mock_chat.assert_called_once_with("hi", user_id="custom_123")


class TestWebhook:
    @patch("app.get_telegram_app")
    def test_webhook_post(self, mock_get_app, client):
        mock_app = MagicMock()
        mock_get_app.return_value = mock_app
        mock_app.bot = MagicMock()

        with patch("app._run_async"):
            resp = client.post("/webhook", json={"update_id": 1})
            assert resp.status_code == 200
