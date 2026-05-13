# Single gunicorn worker is required by the SQLite-on-volume state backend
# (state_manager.py). SQLite WAL mode handles many threads but only one
# writer at a time within a single process; running multiple worker
# processes against the same DB on a volume would surface `database is
# locked` errors. If we ever need horizontal scale-out, migrate to Postgres.
web: gunicorn app:app
