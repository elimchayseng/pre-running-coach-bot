"""Google Calendar integration: OAuth, REST client, plan-to-events sync.

Public API:
    auth.get_access_token()              -> str
    auth.exchange_code_for_tokens(code)  -> dict
    client.insert_event(event)           -> dict
    client.patch_event(event_id, patch)  -> dict
    client.delete_event(event_id)        -> None
    client.list_managed_events(tmin, tmax) -> list[dict]
    client.get_calendar(calendar_id)     -> dict
    sync.sync_plan(state, dry_run=False) -> dict
"""
