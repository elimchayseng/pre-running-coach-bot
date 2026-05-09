"""Strava integration: OAuth, activity fetch/translate, webhook handling.

Public API:
    auth.get_access_token()           -> str
    auth.exchange_code_for_tokens(code) -> dict
    client.get_activity(activity_id)  -> dict
    client.list_activities(after)     -> list[dict]
    translator.activity_to_log_entry(activity) -> dict
    notify.send_activity_ping(chat_id, log_entry) -> None
"""
