"""Shared scaffolding for the OAuth setup CLIs (coros_setup.py and
google_calendar_setup.py).

Both walk a one-time browser OAuth via a one-shot loopback HTTP listener and
print the same style of status lines. This module owns the duplicated pieces —
the status-line helpers and the loopback callback handler with its
favicon-preload guard — so a fix to the callback lands in both flows at once
(issue #57). Previously the favicon guard had to be applied per-script.
"""

from __future__ import annotations

import http.server
import urllib.parse

# ---------- status-line print helpers ----------


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ! {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def _section(title: str) -> None:
    print(f"\n{title}")


# ---------- loopback OAuth callback ----------


def is_oauth_callback(params: dict) -> bool:
    """True only for the real OAuth redirect. Browsers (Chrome especially)
    preload /favicon.ico before following the redirect; a one-shot listener
    that accepted the first request would consume the favicon hit and miss the
    real ?code=/?error= callback. The listener loops until this returns True."""
    return "code" in params or "error" in params


def make_oauth_callback_handler(product_label: str):
    """Return a one-shot BaseHTTPRequestHandler subclass for the OAuth callback.

    On the real callback it stashes the query params on the server instance
    (``server.received``) and 200s a close-this-tab page; non-callback requests
    (favicon preloads) get a 404 and are ignored so the caller's
    ``while server.received is None: server.handle_request()`` loop keeps
    waiting. ``product_label`` only flavors the close-tab page heading.
    """

    class _OAuthHandler(http.server.BaseHTTPRequestHandler):
        """One-shot handler for the OAuth callback. Stashes (code, state) on the
        server instance, then 200s with a friendly close-this-tab message."""

        def do_GET(self):  # noqa: N802 — stdlib name
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if not is_oauth_callback(params):
                self.send_response(404)
                self.end_headers()
                return
            self.server.received = params  # type: ignore[attr-defined]
            body = (
                f"<html><body style='font-family: sans-serif; padding: 2em;'>"
                f"<h2>{product_label} authorization received.</h2>"
                f"<p>You can close this tab and return to the terminal.</p>"
                f"</body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 — stdlib signature
            # Silence default request logging — the user already sees CLI output.
            pass

    return _OAuthHandler
