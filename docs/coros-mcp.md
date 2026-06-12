# COROS MCP integration — spike findings (PR 0)

Verdict: **GO** — headless OAuth fully verified 2026-06-11.

## Server

- Official COROS MCP server, North America region: `https://mcpus.coros.com/mcp`
  (EU: `mcpeu.coros.com`, CN: `mcpcn.coros.com`). Streamable HTTP transport,
  read-only tools.
- Unauthenticated `POST /mcp` → `401` with
  `WWW-Authenticate: Bearer resource_metadata="https://mcpus.coros.com/.well-known/oauth-protected-resource/mcp"`.
  **Quirk:** that protected-resource metadata URL itself returns 401 (server
  bug — RFC 9728 says it must be public). The authorization-server metadata at
  the issuer root works fine, so clients must fall back to issuer-root
  discovery rather than relying on the resource-metadata document.

## OAuth (verified 2026-06-11)

- Authorization-server metadata: `GET https://mcpus.coros.com/.well-known/oauth-authorization-server` → 200.
  - `authorization_endpoint`: `/oauth2/authorize`
  - `token_endpoint`: `/oauth2/token`
  - `registration_endpoint`: `/connect/register`
  - `device_authorization_endpoint`: `/oauth2/device_authorization` (device flow available as a future option)
  - `grant_types_supported`: `authorization_code`, `refresh_token`, `client_credentials`, device code, token exchange
  - `scopes_supported`: `openid`, `mcp.tools`, `offline_access`
  - PKCE: S256 required; `token_endpoint_auth_methods_supported` includes `none` (public clients)
- **Dynamic client registration works**: `POST /connect/register` with
  `token_endpoint_auth_method: "none"`, `grant_types [authorization_code, refresh_token]`,
  scope `mcp.tools offline_access` → `201` with a `client_id` (no secret).
  The registered `client_id` must be persisted alongside tokens for reuse.
- Request scope `mcp.tools offline_access` — `offline_access` is what makes the
  server issue a refresh token.

## Headless model

One-time interactive login (browser, loopback redirect on `localhost:8766`)
produces `access_token` + `refresh_token`. Production then runs fully headless:
refresh via `grant_type=refresh_token` with the persisted public `client_id`.

Spike checklist (all verified 2026-06-11):

- [x] Discovery (issuer-root metadata)
- [x] DCR (201, public client)
- [x] Refresh token issued on auth-code exchange (`expires_in` ≈ **30 days** on the access token)
- [x] Headless replay in a fresh process — `queryDailyHealthData` works with persisted tokens, no browser
- [x] Refresh exchange works — **refresh token ROTATES on every refresh.**
      Consequences for `coros/auth.py`: (1) persist the new refresh token
      atomically *before* returning the access token — a lost write means
      lockout; (2) serialize refreshes with a lock — two concurrent refreshes
      with the same (now-consumed) refresh token would fail the second.
- [x] Fixtures captured to `tests/fixtures/coros/` (9 tools)
- [ ] Refresh still valid ≥25h after issue (run
      `python scripts/coros_setup.py status` tomorrow — low risk given
      30-day access tokens; does not block anything). The throwaway spike
      script was deleted after capture; `coros/` + `scripts/coros_setup.py`
      are the production replacements.

## Tool output parsing notes (for the translator)

- `result.content[0].text` is a **JSON-encoded string** (wrapped in quotes,
  `\n` escapes). Unwrap with `json.loads` before line parsing.
- Date formats differ per tool: `queryDailyHealthData` section headers use
  `yyyyMMdd` (`--- 20260611 ---`); all other tools use ISO `2026-06-11`.
- `queryDailyHealthData` header carries point-in-time `Resting HR` and
  `HRV Baseline`; per-day resting HR comes from `queryRestingHeartRate`
  (today's row is `No data` until tomorrow). Per-day HRV list from
  `queryHrvAssessment` also lags: no entry for today.
- `querySleepData` "Main Sleep" excludes naps; `queryDailyHealthData` sleep
  total includes naps. Durations formatted `7h 32min` / `49 min` / `3h 34min`.
- `queryStressLevel` per-bucket breakdown rows are all `No data` currently —
  only `Average Stress: N (Label)` is usable.
- `queryRecoveryStatus` is point-in-time only (no history).

## Runtime constraints

- `mcp` Python SDK requires Python ≥ 3.10; repo venv upgraded to **3.12**
  (uv-managed standalone build — the Homebrew `python@3.12` bottle is broken on
  Darwin 25.1: its `pyexpat` links a newer `libexpat` symbol than the system
  dylib provides). All 624 existing tests pass on 3.12.
- `mcp` SDK pinned `>=1.9,<2` (1.27.2 installed).
- MCP tool outputs are human-readable **text**, not JSON — the translator
  parses text and stores raw payloads as insurance.

## Decision

Official MCP only — no unofficial-API fallback (user decision). If headless
refresh proves unreliable in practice, the watchdog alerts and
`make coros-reauth-prod` re-auths in one command.
