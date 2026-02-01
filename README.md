# everping

Lightweight personal scheduler/monitor with a simple web UI.

## Requirements

- Python 3.11 or 3.12 (SQLAlchemy 2.0.x is not compatible with Python 3.14)
- OS: primary target Ubuntu 20.04 (Linux). Windows can run the web UI, but the executor uses `/bin/bash`.

## What it does

- Schedule tasks (interval/cron/deadline)
- Run job registry commands from `jobs.json`
- Monitor metrics via `OUT=` stdout lines
- Alerts with suppression + optional push script
- Web UI for tasks, runs, metrics, alerts

## How it works (high-level)

- FastAPI serves HTML pages (`templates/`) for tasks, runs, metrics, alerts.
- APScheduler loads triggers from DB and enqueues runs.
- Worker thread pool picks `PENDING` runs and executes them.
- Command output is persisted to log files; errors and alerts are stored in DB.

## Project structure

```
app/
  main.py       # main app (scheduler/worker/services/routes)
  executor.py   # command runner
  models.py     # SQLAlchemy models
  db.py         # DB engine + sessions
  auth.py       # simple session auth
templates/      # HTML views
static/         # local assets (bootstrap, metrics.js)
jobs.json       # job registry
```

## Quick start (Ubuntu 20.04)

```
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.main
```

Then open `http://127.0.0.1:8000` (or use `ROOT_PATH` if behind nginx).

## Configuration

Environment variables (see `.env.example`):

- `APP_SECRET`: session secret
- `ADMIN_USER`, `ADMIN_PASS`: login credentials
- `DB_URL`: DB connection string (default SQLite)
- `HOST`, `PORT`: bind address
- `ROOT_PATH`: nginx subpath (e.g. `/everping`)
- `TIMEZONE`: scheduler + deadline timezone
- `JOBS_FILE`: job registry file
- `MAX_WORKERS`: worker pool size
- `ALERT_SUPPRESS_SEC`: alert suppression window
- `ALERT_PUSH_*`: push script settings
- `LOG_DIR`: app/run log directory
- `METRICS_DIR`, `METRICS_RETENTION_DAYS`: CSV metrics storage

## Notes

- `deadline_at` expects an ISO time string (local time in `TIMEZONE`).
- For safety, run on localhost or behind a reverse proxy with auth.

## jobs.json

`jobs.json` defines the executable argv list. UI only selects a job.

Example:

```
[
  {
    "id": "cpu-check",
    "label": "cpu检查",
    "cmd": ["/root/sh/task/check_cpu.sh", "-a", "[label]", "-b", "[style]"],
    "style": "secondary"
  }
]
```

Placeholders supported in `cmd`:
`[label]`, `[style]`, `[task_name]` (or `{label}`, `{style}`, `{task_name}`).
