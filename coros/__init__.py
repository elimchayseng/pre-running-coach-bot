"""COROS wearable integration: official COROS MCP server client.

Pulls daily health metrics (sleep, HRV, resting HR, stress, recovery,
training load) from https://mcpus.coros.com/mcp into the daily_health
SQLite table. See docs/coros-mcp.md for the OAuth model and spike findings.
"""
