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
    @patch("health.run_health_checks", return_value={"redis": True, "llm": True})
    def test_health_returns_200_when_all_ok(self, mock_checks, client):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"
        assert data["redis"] == "ok"
        assert data["llm"] == "ok"

    def test_health_contains_bot_name(self, client):
        resp = client.get("/")
        data = resp.get_json()
        assert "PRE" in data["bot"]

    @patch("health.run_health_checks", return_value={"redis": False, "llm": True})
    def test_health_returns_503_when_redis_down(self, mock_checks, client):
        resp = client.get("/")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["status"] == "degraded"
        assert data["redis"] == "fail"


class TestWebhook:
    @patch("app.get_telegram_app")
    def test_webhook_post(self, mock_get_app, client):
        mock_app = MagicMock()
        mock_get_app.return_value = mock_app
        mock_app.bot = MagicMock()

        with patch("app._run_async"):
            resp = client.post("/webhook", json={"update_id": 1})
            assert resp.status_code == 200
