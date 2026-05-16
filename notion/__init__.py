"""Notion mirror — one-way reflection of SQLite state into Notion databases.

SQLite remains the source of truth. Phase 1B writes only; bidirectional sync
is a later phase. See notion/client.py for the API wrapper and
notion/schema.py for the four mirror-database definitions.
"""
