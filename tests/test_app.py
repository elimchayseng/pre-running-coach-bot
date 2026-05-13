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

    @patch("app.threading.Thread")
    @patch("app.get_telegram_app")
    def test_webhook_dispatches_to_background_thread(self, mock_get_app, mock_thread, client):
        """Regression for issue #15: /webhook must ack 200 fast and process
        the update in a background thread, mirroring /strava/webhook. This
        prevents Telegram retries from queuing on slow LLM calls."""
        mock_app = MagicMock()
        mock_get_app.return_value = mock_app
        mock_app.bot = MagicMock()
        instance = MagicMock()
        mock_thread.return_value = instance

        resp = client.post("/webhook", json={"update_id": 42})
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok"}

        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args.kwargs
        assert kwargs.get("daemon") is True
        instance.start.assert_called_once()

    @patch("app.threading.Thread")
    @patch("app.get_telegram_app")
    def test_webhook_returns_200_even_when_processing_fails(self, mock_get_app, mock_thread, client):
        """Regression for issue #15: even if Telegram processing raises in
        the background thread, the webhook must still return 200 so Telegram
        doesn't retry. The thread itself swallows + logs the failure."""
        mock_app = MagicMock()
        mock_get_app.return_value = mock_app
        mock_app.bot = MagicMock()

        # Capture and immediately invoke the thread target to simulate the
        # background work running synchronously, with _run_async configured
        # to raise.
        captured_target = {}

        def fake_thread(target=None, daemon=None, **_):
            captured_target["fn"] = target
            return MagicMock()

        mock_thread.side_effect = fake_thread

        with patch("app._run_async", side_effect=RuntimeError("simulated failure")):
            resp = client.post("/webhook", json={"update_id": 7})
            assert resp.status_code == 200
            # Invoking the captured target must not propagate the exception.
            captured_target["fn"]()


class TestStravaWebhook:
    """The challenge-verify GET and the event-dispatch POST on /strava/webhook."""

    def test_verify_correct_token_echoes_challenge(self, client, monkeypatch):
        monkeypatch.setenv("STRAVA_VERIFY_TOKEN", "secret123")
        resp = client.get(
            "/strava/webhook",
            query_string={
                "hub.mode": "subscribe",
                "hub.challenge": "abc",
                "hub.verify_token": "secret123",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"hub.challenge": "abc"}

    def test_verify_wrong_token_403(self, client, monkeypatch):
        monkeypatch.setenv("STRAVA_VERIFY_TOKEN", "secret123")
        resp = client.get(
            "/strava/webhook",
            query_string={
                "hub.mode": "subscribe",
                "hub.challenge": "abc",
                "hub.verify_token": "WRONG",
            },
        )
        assert resp.status_code == 403

    def test_verify_missing_token_env_403(self, client, monkeypatch):
        """If STRAVA_VERIFY_TOKEN isn't configured, refuse all verify attempts —
        defense against accidentally accepting any request when env was forgotten."""
        monkeypatch.delenv("STRAVA_VERIFY_TOKEN", raising=False)
        resp = client.get(
            "/strava/webhook",
            query_string={
                "hub.mode": "subscribe",
                "hub.challenge": "abc",
                "hub.verify_token": "anything",
            },
        )
        assert resp.status_code == 403

    @patch("threading.Thread")
    def test_event_dispatches_thread_and_returns_200(self, mock_thread, client):
        instance = MagicMock()
        mock_thread.return_value = instance

        payload = {
            "aspect_type": "create",
            "object_type": "activity",
            "object_id": 18186603028,
            "owner_id": 44108500,
        }
        resp = client.post("/strava/webhook", json=payload)

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok"}

        # Thread was created with handle_event as target and started exactly once
        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args.kwargs
        assert kwargs.get("daemon") is True
        # target should be the handler.handle_event function
        target = kwargs.get("target")
        assert target is not None and target.__name__ == "handle_event"
        # The payload is forwarded as the only positional arg
        assert kwargs.get("args") == (payload,)
        instance.start.assert_called_once()

    @patch("threading.Thread")
    def test_event_malformed_body_still_200(self, mock_thread, client):
        """Strava expects 200 within 2s. Even garbage bodies should ack —
        the handler sees an empty payload and skips."""
        resp = client.post("/strava/webhook", data="not-json", content_type="text/plain")
        assert resp.status_code == 200
        # Empty payload still dispatches; handler.handle_event filters internally
        mock_thread.assert_called_once()
