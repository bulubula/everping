from __future__ import annotations
import json
import os
import platform
import threading
import time
import logging
import csv
from pathlib import Path
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Depends, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from concurrent.futures import ThreadPoolExecutor
import psutil

from app.config import settings
from app.db import Base, engine, get_db, SessionLocal
from app.models import Task, Trigger, Run, Metric, AlertState, Alert
from app.auth import verify_login, require_login, login, logout
from app.executor import run_command_killpg, run_argv_killpg

# -------------------- jobs registry --------------------

_jobs_lock = threading.Lock()
_jobs_loaded = False
_jobs: dict[str, dict[str, str | list[str]]] = {}
_jobs_list: list[dict[str, str | list[str]]] = []
_jobs_error: str | None = None


def _load_jobs_from_file(path: str) -> tuple[dict[str, dict[str, str | list[str]]], list[dict[str, str | list[str]]]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "jobs" in data:
        items = data["jobs"]
    else:
        items = data

    if not isinstance(items, list):
        raise ValueError("jobs.json must be a list or {\"jobs\": [...]} object")

    job_map: dict[str, dict[str, str | list[str]]] = {}
    job_list: list[dict[str, str | list[str]]] = []

    for raw in items:
        if not isinstance(raw, dict):
            continue
        job_id = str(raw.get("id") or raw.get("label") or "").strip()
        cmd = raw.get("cmd")
        if not job_id or not isinstance(cmd, list):
            continue
        label_val = str(raw.get("label") or "")
        style_val = str(raw.get("style") or "")
        cmd_list = []
        for x in cmd:
            token = str(x)
            if token in ("[label]", "{label}"):
                cmd_list.append(label_val)
            elif token in ("[style]", "{style}"):
                cmd_list.append(style_val)
            else:
                cmd_list.append(token)
        job = {
            "id": job_id,
            "label": job_id,
            "cmd": cmd_list,
            "style": "",
        }
        job_map[job_id] = job
        job_list.append(job)

    return job_map, job_list


def reload_jobs() -> tuple[bool, str | None]:
    global _jobs_loaded, _jobs, _jobs_list, _jobs_error
    path = settings.JOBS_FILE
    if not os.path.exists(path):
        with _jobs_lock:
            _jobs_loaded = True
            _jobs = {}
            _jobs_list = []
            _jobs_error = f"jobs file not found: {path}"
        return False, _jobs_error

    try:
        job_map, job_list = _load_jobs_from_file(path)
    except Exception as e:
        with _jobs_lock:
            _jobs_error = f"failed to load jobs: {e}"
        return False, _jobs_error

    with _jobs_lock:
        _jobs_loaded = True
        _jobs = job_map
        _jobs_list = job_list
        _jobs_error = None
    return True, None


def _ensure_jobs_loaded() -> None:
    if not _jobs_loaded:
        reload_jobs()


def list_jobs() -> list[dict[str, str | list[str]]]:
    _ensure_jobs_loaded()
    with _jobs_lock:
        return list(_jobs_list)


def get_job(job_id: str | None) -> dict[str, str | list[str]] | None:
    if not job_id:
        return None
    _ensure_jobs_loaded()
    with _jobs_lock:
        return _jobs.get(job_id)


def last_jobs_error() -> str | None:
    _ensure_jobs_loaded()
    with _jobs_lock:
        return _jobs_error


# -------------------- utilities --------------------


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

BASE_DIR = Path(__file__).resolve().parent.parent
METRICS_DIR_PATH = Path(settings.METRICS_DIR)
if not METRICS_DIR_PATH.is_absolute():
    METRICS_DIR_PATH = (BASE_DIR / METRICS_DIR_PATH).resolve()

def setup_logging() -> None:
    ensure_dir(settings.LOG_DIR)
    log_path = os.path.join(settings.LOG_DIR, settings.APP_LOG_NAME)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)


def now_utc() -> datetime:
    return datetime.utcnow()


def now_local_naive() -> datetime:
    return datetime.now(ZoneInfo(settings.TIMEZONE)).replace(tzinfo=None)


def parse_local_naive(dt_str: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(ZoneInfo(settings.TIMEZONE)).replace(tzinfo=None)

def format_local_display(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    dt_local = dt.astimezone(ZoneInfo(settings.TIMEZONE)).replace(tzinfo=None)
    return dt_local.isoformat(sep=" ", timespec="microseconds")


def format_duration_display(started: datetime | None, finished: datetime | None) -> str:
    if not started:
        return ""
    end = finished or now_utc()
    total = max(int((end - started).total_seconds()), 0)
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_status_detail(run: Run, max_len: int = 120) -> str:
    if run.status == "SUCCESS":
        return ""
    if run.error_message:
        text = " ".join(run.error_message.split())
        if len(text) > max_len:
            text = text[:max_len].rstrip() + "..."
        return text
    if run.exit_code is not None:
        return f"exit_code={run.exit_code}"
    return ""


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "N/A"


def _format_bytes_gb(n: int | float) -> str:
    return f"{(float(n) / (1024 ** 3)):.3f} G"


def _format_bytes_auto(n: int | float) -> str:
    value = float(n)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.2f} {units[idx]}"


def _format_percent(v: float) -> str:
    return f"{float(v):.2f}%"


def _format_uptime(seconds: float) -> str:
    total = max(int(seconds), 0)
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    return f"{days}d{hours}h{minutes}m"

def _safe_loadavg() -> tuple[str, str, str]:
    try:
        one, five, fifteen = os.getloadavg()
        return f"{one:.2f}", f"{five:.2f}", f"{fifteen:.2f}"
    except Exception:
        return "N/A", "N/A", "N/A"


def _read_cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    _, value = line.split(":", 1)
                    model = value.strip()
                    if model:
                        return model
    except Exception:
        pass

    proc = platform.processor().strip()
    if proc:
        return proc
    return "unknown"


def _collect_disk_usage(path: str) -> dict[str, object]:
    item: dict[str, object] = {
        "path": path,
        "available": False,
        "total": "N/A",
        "used": "N/A",
        "free": "N/A",
        "percent": "N/A",
        "percent_float": 0.0,
    }
    try:
        usage = psutil.disk_usage(path)
    except Exception:
        return item

    item["available"] = True
    item["total"] = _format_bytes_gb(usage.total)
    item["used"] = _format_bytes_gb(usage.used)
    item["free"] = _format_bytes_gb(usage.free)
    item["percent"] = _format_percent(usage.percent)
    item["percent_float"] = float(usage.percent)
    return item


def _collect_nic_traffic(nic: str, counters: dict[str, object]) -> dict[str, str]:
    item = {"name": nic, "rx": "N/A", "tx": "N/A"}
    stat = counters.get(nic)
    if not stat:
        return item
    try:
        item["rx"] = _format_bytes_auto(stat.bytes_recv)
        item["tx"] = _format_bytes_auto(stat.bytes_sent)
    except Exception:
        return item
    return item


def _collect_home_system_info(request: Request) -> dict[str, object]:
    info: dict[str, object] = {
        "client_ip": _client_ip(request),
        "server_time": now_local_naive().strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": "N/A",
        "cpu_model": _read_cpu_model(),
        "disks": [],
        "loadavg": "N/A N/A N/A",
        "nics": [],
    }

    try:
        uptime_sec = time.time() - psutil.boot_time()
        info["uptime"] = _format_uptime(uptime_sec)
    except Exception:
        pass

    disk_paths = ["/", "/mnt/sda", "/mnt/sdc", "/mnt/sdd"]
    info["disks"] = [_collect_disk_usage(path) for path in disk_paths]

    one, five, fifteen = _safe_loadavg()
    info["loadavg"] = f"{one} {five} {fifteen}"

    nic_counters: dict[str, object] = {}
    try:
        nic_counters = psutil.net_io_counters(pernic=True)
    except Exception:
        nic_counters = {}
    info["nics"] = [
        _collect_nic_traffic("br0", nic_counters),
        _collect_nic_traffic("lo", nic_counters),
    ]
    return info

def parse_out_line(stdout_text: str) -> list[str]:
    lines = [ln.rstrip("\n") for ln in stdout_text.splitlines()]
    out_lines = [ln for ln in lines if ln.startswith("OUT=")]
    if not out_lines:
        return []
    payload = out_lines[-1][4:]
    if payload == "":
        return []
    return payload.split("\t")


def parse_metrics_tokens(tokens: list[str]) -> list[tuple[str, float]]:
    pairs: list[tuple[str, float]] = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        if "=" in t:
            k, v = t.split("=", 1)
            try:
                pairs.append((k.strip(), float(v.strip())))
            except ValueError:
                continue
        else:
            try:
                pairs.append(("value", float(t)))
            except ValueError:
                continue
    return pairs

def _metrics_file(task_id: int) -> str:
    ensure_dir(str(METRICS_DIR_PATH))
    return str(METRICS_DIR_PATH / f"task_{task_id}.csv")

def _prune_metrics_file(path: str) -> None:
    days = int(settings.METRICS_RETENTION_DAYS)
    if days <= 0 or not os.path.exists(path):
        return
    cutoff = now_utc() - timedelta(days=days)
    try:
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for r in reader:
                if len(r) < 4:
                    continue
                try:
                    ts = datetime.fromisoformat(r[0])
                except ValueError:
                    continue
                if ts >= cutoff:
                    rows.append(r)
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
    except Exception:
        pass

def write_metrics_csv(task: Task, pairs: list[tuple[str, float]]) -> None:
    if not pairs:
        return
    path = _metrics_file(task.id)
    ensure_dir(str(METRICS_DIR_PATH))
    ts = now_local_naive().replace(microsecond=0).isoformat()
    try:
        with open(path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            for k, v in pairs:
                writer.writerow([ts, task.id, task.name, k, v])
    except Exception:
        pass
    _prune_metrics_file(path)


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


# -------------------- services --------------------


def enqueue_run(db: Session, task_id: int, trigger_id: int | None) -> int:
    r = Run(task_id=task_id, trigger_id=trigger_id, status="PENDING", scheduled_at=now_utc())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r.id


def create_task(db: Session, task: Task) -> tuple[Task | None, str | None]:
    db.add(task)
    try:
        db.commit()
        db.refresh(task)
        return task, None
    except IntegrityError as e:
        db.rollback()
        msg = str(getattr(e, "orig", e))
        return None, f"Failed to create task: {msg}"


def holiday_allowed(policy: str) -> bool:
    if policy == "NONE":
        return True
    try:
        import chinese_calendar
        from datetime import date
        today = date.today()
        is_workday = chinese_calendar.is_workday(today)
        is_holiday = chinese_calendar.is_holiday(today)
        if policy == "CN_WORKDAY_ONLY":
            return is_workday
        if policy == "SKIP_CN_HOLIDAY":
            return not is_holiday
        if policy == "SKIP_CN_WORKDAY":
            return not is_workday
        return True
    except Exception:
        return True


def acquire_task_mutex(db: Session, task_id: int, run_id: int) -> bool:
    q = (
        select(Run.id)
        .where(Run.task_id == task_id, Run.status == "RUNNING", Run.id != run_id)
        .limit(1)
    )
    exists = db.execute(q).scalar_one_or_none()
    return exists is None


def _cleanup_old_run_logs() -> None:
    # keep last LOG_BACKUP_COUNT days
    keep_days = max(int(settings.LOG_BACKUP_COUNT), 1)
    cutoff = now_local_naive().date() - timedelta(days=keep_days)
    try:
        for name in os.listdir(settings.LOG_DIR):
            if not name.startswith("run_") or not name.endswith(".log"):
                continue
            # run_YYYYMMDD.out.log / run_YYYYMMDD.err.log
            parts = name.split(".")
            if len(parts) < 3:
                continue
            date_part = parts[0].replace("run_", "")
            try:
                file_date = datetime.strptime(date_part, "%Y%m%d").date()
            except ValueError:
                continue
            if file_date < cutoff:
                try:
                    os.remove(os.path.join(settings.LOG_DIR, name))
                except Exception:
                    pass
    except Exception:
        pass

def write_run_logs(task: Task, run_id: int, stdout: str, stderr: str) -> tuple[str, str]:
    ensure_dir(settings.LOG_DIR)
    day = now_local_naive().strftime("%Y%m%d")
    out_path = os.path.join(settings.LOG_DIR, f"run_{day}.out.log")
    err_path = os.path.join(settings.LOG_DIR, f"run_{day}.err.log")
    header = f"[{now_local_naive().isoformat(sep=' ', timespec='seconds')}] task={task.name} run={run_id}\n"
    if stdout:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(header)
            f.write(stdout)
            if not stdout.endswith("\n"):
                f.write("\n")
    if stderr:
        with open(err_path, "a", encoding="utf-8") as f:
            f.write(header)
            f.write(stderr)
            if not stderr.endswith("\n"):
                f.write("\n")
    _cleanup_old_run_logs()
    return out_path, err_path


def maybe_send_alert(db: Session, task: Task, alert_type: str, message: str) -> bool:
    now = now_utc()
    st = db.execute(
        select(AlertState).where(AlertState.task_id == task.id, AlertState.alert_type == alert_type)
    ).scalar_one_or_none()

    suppressed = False
    if st and st.last_sent_at and (now - st.last_sent_at).total_seconds() < settings.ALERT_SUPPRESS_SEC:
        suppressed = True

    if not st:
        st = AlertState(task_id=task.id, alert_type=alert_type, last_sent_at=now)
        db.add(st)
    else:
        st.last_sent_at = now

    db.add(Alert(task_id=task.id, alert_type=alert_type, message=message, suppressed=1 if suppressed else 0))
    db.commit()

    if suppressed:
        return False

    # fire push script (non-blocking)
    try:
        os.system(
            f"nohup python3 {_sh_quote(settings.ALERT_PUSH_SCRIPT)} "
            f"{_sh_quote(message)} -t {_sh_quote(settings.ALERT_PUSH_TITLE)} "
            f"-g {_sh_quote(settings.ALERT_PUSH_GROUP)} -l {_sh_quote(settings.ALERT_PUSH_LEVEL)} "
            f">/dev/null 2>&1 &"
        )
    except Exception:
        pass
    return True


def execute_one_run(db: Session, run_id: int) -> None:
    now = now_utc()
    claim = (
        update(Run)
        .where(Run.id == run_id, Run.status == "PENDING")
        .values(status="RUNNING", started_at=now)
    )
    result = db.execute(claim)
    if result.rowcount == 0:
        db.commit()
        return
    db.commit()
    run = db.get(Run, run_id)
    if not run:
        return

    # zombie recovery: mark long-running runs as FAILED
    try:
        cutoff = now_utc() - timedelta(seconds=int(settings.RUN_ZOMBIE_SEC))
        db.execute(
            update(Run)
            .where(Run.status == "RUNNING", Run.started_at < cutoff)
            .values(status="FAILED", finished_at=now_utc(), error_message="Zombie run auto-failed")
        )
        db.commit()
    except Exception:
        db.rollback()

    task = db.get(Task, run.task_id)
    if not task or task.enabled != 1:
        run.status = "SKIPPED"
        run.finished_at = now_utc()
        db.commit()
        return

    if not acquire_task_mutex(db, task.id, run.id):
        run.status = "FAILED"
        run.finished_at = now_utc()
        run.exit_code = 99
        run.error_message = "Task is already RUNNING (non-reentrant)."
        db.commit()
        maybe_send_alert(db, task, "reentry", f"{task.name}: reentry blocked")
        return

    try:
        _execute_single(db, run, task, [])
    except Exception as e:
        run.status = "FAILED"
        run.finished_at = now_utc()
        run.exit_code = 98
        run.error_message = f"Internal error: {e}"
        db.commit()
        maybe_send_alert(db, task, "internal_error", f"{task.name}: internal error: {e}")


def _execute_single(db: Session, run: Run, task: Task, args: list[str]) -> tuple[int, list[str], str, str, bool]:
    if task.job_id:
        job = get_job(task.job_id)
        if not job:
            run.status = "FAILED"
            run.finished_at = now_utc()
            run.exit_code = 97
            run.error_message = f"Job not found: {task.job_id}"
            db.commit()
            maybe_send_alert(db, task, "job_missing", f"{task.name}: job not found: {task.job_id}")
            return 97, [], "", f"job not found: {task.job_id}", False
        argv = list(job["cmd"])
        argv = [task.name if t in ("[task_name]", "{task_name}") else t for t in argv]
        if args:
            argv.extend(args)
        res = run_argv_killpg(argv, timeout_sec=settings.DEFAULT_TIMEOUT_SEC)
    else:
        cmd = task.command_template.strip()
        if args:
            cmd = cmd + " " + " ".join([_sh_quote(a) for a in args])
        res = run_command_killpg(cmd, timeout_sec=settings.DEFAULT_TIMEOUT_SEC)

    skip_logs = task.type == "monitor" and (not res.timed_out) and res.exit_code == 0
    if not skip_logs:
        out_path, err_path = write_run_logs(task, run.id, res.stdout, res.stderr)
        run.stdout_path = out_path
        run.stderr_path = err_path

    if res.timed_out:
        run.status = "TIMEOUT"
    else:
        run.status = "SUCCESS" if res.exit_code == 0 else "FAILED"
    run.exit_code = res.exit_code
    run.finished_at = now_utc()
    db.commit()

    tokens = parse_out_line(res.stdout or "")
    if task.type == "monitor" and tokens:
        write_metrics_csv(task, parse_metrics_tokens(tokens))

    if task.type == "monitor" and run.status == "SUCCESS":
        db.delete(run)
        db.commit()
        return res.exit_code, tokens, res.stdout, res.stderr, res.timed_out

    if run.status in ("FAILED", "TIMEOUT"):
        maybe_send_alert(db, task, "exec_failed", f"{task.name}: status={run.status} code={run.exit_code}")

    return res.exit_code, tokens, res.stdout, res.stderr, res.timed_out


# -------------------- scheduler --------------------


class AppScheduler:
    def __init__(self) -> None:
        self.sched = BackgroundScheduler(timezone=settings.TIMEZONE)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._reload_jobs()
        self.sched.start()
        self._started = True

    def shutdown(self) -> None:
        try:
            self.sched.shutdown(wait=False)
        except Exception:
            pass

    def _reload_jobs(self) -> None:
        self.sched.remove_all_jobs()
        with SessionLocal() as db:
            triggers = db.execute(select(Trigger).where(Trigger.enabled == 1)).scalars().all()

        for t in triggers:
            if t.trigger_type == "cron" and t.cron_expr:
                parts = t.cron_expr.strip().split()
                if len(parts) != 5:
                    continue
                minute, hour, day, month, dow = parts
                trig = CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow)
                self.sched.add_job(self._fire, trig, args=[t.id], id=f"trigger_{t.id}", replace_existing=True)

            elif t.trigger_type in ("interval", "monitor") and t.interval_sec:
                trig = IntervalTrigger(seconds=int(t.interval_sec))
                self.sched.add_job(self._fire, trig, args=[t.id], id=f"trigger_{t.id}", replace_existing=True)

            elif t.trigger_type == "deadline" and t.deadline_config:
                try:
                    cfg = json.loads(t.deadline_config)
                    interval_hours = int(cfg.get("interval_hours", 6))
                except Exception:
                    interval_hours = 6
                trig = IntervalTrigger(seconds=max(interval_hours, 1) * 3600)
                self.sched.add_job(
                    self._fire, trig, args=[t.id], id=f"trigger_{t.id}", replace_existing=True
                )

    def _fire(self, trigger_id: int) -> None:
        with SessionLocal() as db:
            t = db.get(Trigger, trigger_id)
            if not t or t.enabled != 1:
                return
            if not holiday_allowed(t.holiday_policy):
                return
            task = t.task
            if not task or task.enabled != 1:
                return
            if t.trigger_type == "deadline":
                try:
                    cfg = json.loads(t.deadline_config or "{}")
                    deadline_at = cfg.get("deadline_at")
                    start_before_days = int(cfg.get("start_before_days", 1))
                except Exception:
                    return
                if not deadline_at:
                    return
                deadline_dt = parse_local_naive(deadline_at)
                if not deadline_dt:
                    return
                start_dt = deadline_dt - timedelta(days=start_before_days)
                now = now_local_naive()
                if now < start_dt:
                    return
                if now > deadline_dt:
                    t.enabled = 0
                    db.commit()
                    return
            enqueue_run(db, task.id, t.id)


# -------------------- worker pool --------------------


class WorkerPool:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=settings.MAX_WORKERS)
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with SessionLocal() as db:
                    q = (
                        select(Run.id)
                        .where(Run.status == "PENDING")
                        .order_by(Run.scheduled_at)
                        .limit(settings.MAX_WORKERS)
                    )
                    run_ids = [r for r in db.execute(q).scalars().all()]

                for rid in run_ids:
                    self._executor.submit(self._run_one, rid)

            except Exception:
                pass

            time.sleep(0.5)

    def _run_one(self, run_id: int) -> None:
        with SessionLocal() as db:
            execute_one_run(db, run_id)


# -------------------- web app --------------------


Base.metadata.create_all(bind=engine)

scheduler = AppScheduler()
workers = WorkerPool()


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    reload_jobs()
    scheduler.start()
    workers.start()
    try:
        yield
    finally:
        scheduler.shutdown()
        workers.stop()


_root_path = settings.ROOT_PATH.strip()
if _root_path == "/":
    _root_path = ""
elif _root_path:
    _root_path = "/" + _root_path.strip("/")

app = FastAPI(lifespan=lifespan, root_path=_root_path)
app.add_middleware(SessionMiddleware, secret_key=settings.APP_SECRET)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _guard(request: Request):
    if not require_login(request):
        return RedirectResponse(request.url_for("login_page"), status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    redir = _guard(request)
    if redir:
        return redir
    sysinfo = _collect_home_system_info(request)
    return templates.TemplateResponse("index.html", {"request": request, "sysinfo": sysinfo})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_login(username, password):
        login(request, username)
        return RedirectResponse(request.url_for("index"), status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})


@app.post("/logout")
def do_logout(request: Request):
    logout(request)
    return RedirectResponse(request.url_for("login_page"), status_code=303)


@app.get("/tasks", response_class=HTMLResponse)
def tasks(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    items = db.execute(select(Task).order_by(Task.id.desc())).scalars().all()
    return templates.TemplateResponse(
        "tasks.html",
        {"request": request, "tasks": items, "jobs_error": last_jobs_error()},
    )


@app.get("/tasks/new", response_class=HTMLResponse)
def task_new(request: Request):
    redir = _guard(request)
    if redir:
        return redir
    return templates.TemplateResponse(
        "task_new.html",
        {"request": request, "error": None, "jobs": list_jobs()},
    )


@app.post("/tasks/new")
def task_new_post(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    job_id: str = Form(""),
    enabled: int = Form(1),
    remark: str = Form(""),
    trigger_type: str = Form("none"),
    interval_sec: str = Form(""),
    cron_expr: str = Form(""),
    deadline_at: str = Form(""),
    start_before_days: str = Form("1"),
    interval_hours: str = Form("6"),
    holiday_policy: str = Form("NONE"),
    db: Session = Depends(get_db),
):
    redir = _guard(request)
    if redir:
        return redir

    job_id = job_id.strip() or None
    if not job_id:
        return templates.TemplateResponse(
            "task_new.html",
            {"request": request, "error": "Please select a job.", "jobs": list_jobs()},
        )
    if job_id and not get_job(job_id):
        return templates.TemplateResponse(
            "task_new.html",
            {"request": request, "error": "Job not found. Please reload jobs.", "jobs": list_jobs()},
        )
    t = Task(
        name=name.strip(),
        type=type.strip(),
        command_template="",
        job_id=job_id,
        enabled=int(enabled),
        timeout_sec_default=settings.DEFAULT_TIMEOUT_SEC,
        remark=remark.strip() or None,
    )
    _, err = create_task(db, t)
    if err:
        return templates.TemplateResponse(
            "task_new.html",
            {"request": request, "error": err, "jobs": list_jobs()},
        )

    trig_type = trigger_type.strip()
    if trig_type and trig_type != "none":
        try:
            if trig_type == "interval":
                sec = int(interval_sec)
                tr = Trigger(task_id=t.id, trigger_type="interval", interval_sec=sec, holiday_policy=holiday_policy, enabled=1)
            elif trig_type == "cron":
                expr = cron_expr.strip()
                tr = Trigger(task_id=t.id, trigger_type="cron", cron_expr=expr, holiday_policy=holiday_policy, enabled=1)
            elif trig_type == "deadline":
                cfg = json.dumps(
                    {
                        "deadline_at": deadline_at.strip(),
                        "start_before_days": int(start_before_days),
                        "interval_hours": int(interval_hours),
                    },
                    ensure_ascii=False,
                )
                tr = Trigger(
                    task_id=t.id,
                    trigger_type="deadline",
                    deadline_config=cfg,
                    holiday_policy=holiday_policy,
                    enabled=1,
                )
            else:
                tr = None
        except Exception:
            tr = None

        if tr is None:
            return templates.TemplateResponse(
                "task_new.html",
                {"request": request, "error": "Invalid trigger configuration.", "jobs": list_jobs()},
            )
        db.add(tr)
        db.commit()
        scheduler._reload_jobs()
    return RedirectResponse(request.url_for("tasks"), status_code=303)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: int, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    t = db.get(Task, task_id)
    trigs = db.execute(select(Trigger).where(Trigger.task_id == task_id).order_by(Trigger.id.desc())).scalars().all()
    trig_cfg: dict[int, dict[str, str]] = {}
    for tr in trigs:
        if tr.trigger_type == "deadline" and tr.deadline_config:
            try:
                cfg = json.loads(tr.deadline_config)
            except Exception:
                cfg = {}
            trig_cfg[tr.id] = {
                "deadline_at": str(cfg.get("deadline_at", "")),
                "start_before_days": str(cfg.get("start_before_days", "")),
                "interval_hours": str(cfg.get("interval_hours", "")),
            }
        else:
            trig_cfg[tr.id] = {"deadline_at": tr.deadline_config or "", "start_before_days": "", "interval_hours": ""}
    return templates.TemplateResponse(
        "task_detail.html",
        {
            "request": request,
            "task": t,
            "triggers": trigs,
            "job": get_job(t.job_id) if t else None,
            "jobs": list_jobs(),
            "error": None,
            "trig_cfg": trig_cfg,
        },
    )

@app.post("/tasks/{task_id}/edit")
def task_edit(
    request: Request,
    task_id: int,
    type: str = Form(...),
    job_id: str = Form(""),
    enabled: int = Form(1),
    remark: str = Form(""),
    db: Session = Depends(get_db),
):
    redir = _guard(request)
    if redir:
        return redir
    t = db.get(Task, task_id)
    if not t:
        return RedirectResponse(request.url_for("tasks"), status_code=303)

    job_id = job_id.strip() or None
    if not job_id or not get_job(job_id):
        trigs = db.execute(select(Trigger).where(Trigger.task_id == task_id).order_by(Trigger.id.desc())).scalars().all()
        trig_cfg: dict[int, dict[str, str]] = {}
        for tr in trigs:
            if tr.trigger_type == "deadline" and tr.deadline_config:
                try:
                    cfg = json.loads(tr.deadline_config)
                except Exception:
                    cfg = {}
                trig_cfg[tr.id] = {
                    "deadline_at": str(cfg.get("deadline_at", "")),
                    "start_before_days": str(cfg.get("start_before_days", "")),
                    "interval_hours": str(cfg.get("interval_hours", "")),
                }
            else:
                trig_cfg[tr.id] = {"deadline_at": tr.deadline_config or "", "start_before_days": "", "interval_hours": ""}
        return templates.TemplateResponse(
            "task_detail.html",
            {
                "request": request,
                "task": t,
                "triggers": trigs,
                "job": get_job(t.job_id) if t else None,
                "jobs": list_jobs(),
                "error": "Please select a valid job.",
                "trig_cfg": trig_cfg,
            },
        )

    t.type = type.strip()
    t.job_id = job_id
    t.enabled = int(enabled)
    t.remark = remark.strip() or None
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        trigs = db.execute(select(Trigger).where(Trigger.task_id == task_id).order_by(Trigger.id.desc())).scalars().all()
        trig_cfg: dict[int, dict[str, str]] = {}
        for tr in trigs:
            if tr.trigger_type == "deadline" and tr.deadline_config:
                try:
                    cfg = json.loads(tr.deadline_config)
                except Exception:
                    cfg = {}
                trig_cfg[tr.id] = {
                    "deadline_at": str(cfg.get("deadline_at", "")),
                    "start_before_days": str(cfg.get("start_before_days", "")),
                    "interval_hours": str(cfg.get("interval_hours", "")),
                }
            else:
                trig_cfg[tr.id] = {"deadline_at": tr.deadline_config or "", "start_before_days": "", "interval_hours": ""}
        return templates.TemplateResponse(
            "task_detail.html",
            {
                "request": request,
                "task": t,
                "triggers": trigs,
                "job": get_job(t.job_id) if t else None,
                "jobs": list_jobs(),
                "error": f"Failed to update task: {e}",
                "trig_cfg": trig_cfg,
            },
        )

    scheduler._reload_jobs()
    return RedirectResponse(request.url_for("task_detail", task_id=task_id), status_code=303)


@app.post("/jobs/reload")
def jobs_reload(request: Request):
    redir = _guard(request)
    if redir:
        return redir
    reload_jobs()
    return RedirectResponse(request.url_for("tasks"), status_code=303)


@app.post("/tasks/{task_id}/trigger/interval")
def add_interval_trigger(
    request: Request,
    task_id: int,
    interval_sec: int = Form(...),
    holiday_policy: str = Form("NONE"),
    db: Session = Depends(get_db),
):
    redir = _guard(request)
    if redir:
        return redir
    tr = Trigger(task_id=task_id, trigger_type="interval", interval_sec=int(interval_sec), holiday_policy=holiday_policy, enabled=1)
    db.add(tr)
    db.commit()
    scheduler.start()
    scheduler._reload_jobs()
    return RedirectResponse(request.url_for("task_detail", task_id=task_id), status_code=303)


@app.post("/tasks/{task_id}/trigger/cron")
def add_cron_trigger(
    request: Request,
    task_id: int,
    cron_expr: str = Form(...),
    holiday_policy: str = Form("NONE"),
    db: Session = Depends(get_db),
):
    redir = _guard(request)
    if redir:
        return redir
    tr = Trigger(task_id=task_id, trigger_type="cron", cron_expr=cron_expr.strip(), holiday_policy=holiday_policy, enabled=1)
    db.add(tr)
    db.commit()
    scheduler._reload_jobs()
    return RedirectResponse(request.url_for("task_detail", task_id=task_id), status_code=303)



@app.post("/tasks/{task_id}/run")
def run_now(request: Request, task_id: int, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    enqueue_run(db, task_id, None)
    return RedirectResponse(request.url_for("runs"), status_code=303)

@app.post("/tasks/{task_id}/delete")
def task_delete(request: Request, task_id: int, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    t = db.get(Task, task_id)
    if t:
        db.delete(t)
        db.commit()
        scheduler._reload_jobs()
    return RedirectResponse(request.url_for("tasks"), status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    items = db.execute(select(Run).order_by(Run.id.desc()).limit(200)).scalars().all()
    for r in items:
        r.started_at_local = format_local_display(r.started_at)
        r.finished_at_local = format_local_display(r.finished_at)
        r.duration_display = format_duration_display(r.started_at, r.finished_at)
        r.status_detail = format_status_detail(r)
    tasks = {t.id: t for t in db.execute(select(Task)).scalars().all()}
    return templates.TemplateResponse("runs.html", {"request": request, "runs": items, "tasks": tasks})

@app.post("/runs/clear")
def runs_clear(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    db.query(Run).delete()
    db.commit()
    return RedirectResponse(request.url_for("runs"), status_code=303)


@app.get("/metrics", response_class=HTMLResponse)
def metrics(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    rows = []
    try:
        if METRICS_DIR_PATH.is_dir():
            for name in os.listdir(METRICS_DIR_PATH):
                if not name.startswith("task_") or not name.endswith(".csv"):
                    continue
                path = os.path.join(METRICS_DIR_PATH, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        reader = csv.reader(f)
                        for r in reader:
                            if len(r) < 5:
                                continue
                            rows.append(
                                {
                                    "ts": r[0],
                                    "task_id": r[1],
                                    "task_name": r[2],
                                    "key": r[3],
                                    "value": float(r[4]),
                                }
                            )
                except Exception:
                    continue
    except Exception:
        rows = []
    rows.sort(key=lambda x: x["ts"])
    chart_rows = rows
    return templates.TemplateResponse(
        "metrics.html",
        {
            "request": request,
            "chart_rows": json.dumps(chart_rows, ensure_ascii=False),
            "retention_days": settings.METRICS_RETENTION_DAYS,
            "metrics_dir": str(METRICS_DIR_PATH),
        },
    )

@app.post("/metrics/clear")
def metrics_clear(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    try:
        if METRICS_DIR_PATH.is_dir():
            for name in os.listdir(METRICS_DIR_PATH):
                if name.startswith("task_") and name.endswith(".csv"):
                    os.remove(os.path.join(METRICS_DIR_PATH, name))
    except Exception:
        pass
    return RedirectResponse(request.url_for("metrics"), status_code=303)


@app.get("/alerts", response_class=HTMLResponse)
def alerts(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    items = db.execute(select(Alert).order_by(Alert.id.desc()).limit(200)).scalars().all()
    tasks = {t.id: t for t in db.execute(select(Task)).scalars().all()}
    return templates.TemplateResponse("alerts.html", {"request": request, "alerts": items, "tasks": tasks})

@app.post("/alerts/clear")
def alerts_clear(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    db.query(Alert).delete()
    db.commit()
    return RedirectResponse(request.url_for("alerts"), status_code=303)


@app.post("/tasks/{task_id}/trigger/deadline")
def add_deadline_trigger(
    request: Request,
    task_id: int,
    deadline_at: str = Form(...),
    start_before_days: int = Form(1),
    interval_hours: int = Form(6),
    holiday_policy: str = Form("NONE"),
    db: Session = Depends(get_db),
):
    redir = _guard(request)
    if redir:
        return redir
    cfg = json.dumps(
        {
            "deadline_at": deadline_at.strip(),
            "start_before_days": int(start_before_days),
            "interval_hours": int(interval_hours),
        },
        ensure_ascii=False,
    )
    tr = Trigger(
        task_id=task_id,
        trigger_type="deadline",
        deadline_config=cfg,
        holiday_policy=holiday_policy,
        enabled=1,
    )
    db.add(tr)
    db.commit()
    scheduler._reload_jobs()
    return RedirectResponse(request.url_for("task_detail", task_id=task_id), status_code=303)

@app.post("/tasks/{task_id}/trigger/add")
def add_trigger(
    request: Request,
    task_id: int,
    trigger_type: str = Form(...),
    interval_sec: str = Form(""),
    cron_expr: str = Form(""),
    deadline_at: str = Form(""),
    start_before_days: str = Form("1"),
    interval_hours: str = Form("6"),
    holiday_policy: str = Form("NONE"),
    db: Session = Depends(get_db),
):
    redir = _guard(request)
    if redir:
        return redir
    tr = None
    if trigger_type == "interval":
        tr = Trigger(
            task_id=task_id,
            trigger_type="interval",
            interval_sec=int(interval_sec),
            holiday_policy=holiday_policy,
            enabled=1,
        )
    elif trigger_type == "cron":
        tr = Trigger(
            task_id=task_id,
            trigger_type="cron",
            cron_expr=cron_expr.strip(),
            holiday_policy=holiday_policy,
            enabled=1,
        )
    elif trigger_type == "deadline":
        cfg = json.dumps(
            {
                "deadline_at": deadline_at.strip(),
                "start_before_days": int(start_before_days),
                "interval_hours": int(interval_hours),
            },
            ensure_ascii=False,
        )
        tr = Trigger(
            task_id=task_id,
            trigger_type="deadline",
            deadline_config=cfg,
            holiday_policy=holiday_policy,
            enabled=1,
        )
    if tr:
        db.add(tr)
        db.commit()
        scheduler._reload_jobs()
    return RedirectResponse(request.url_for("task_detail", task_id=task_id), status_code=303)

@app.post("/triggers/{trigger_id}/edit")
def trigger_edit(
    request: Request,
    trigger_id: int,
    task_id: int = Form(...),
    trigger_type: str = Form(""),
    cron_expr: str = Form(""),
    interval_sec: str = Form(""),
    deadline_at: str = Form(""),
    start_before_days: str = Form(""),
    interval_hours: str = Form(""),
    holiday_policy: str = Form("NONE"),
    enabled: int = Form(1),
    db: Session = Depends(get_db),
):
    redir = _guard(request)
    if redir:
        return redir
    tr = db.get(Trigger, trigger_id)
    if not tr:
        return RedirectResponse(request.url_for("task_detail", task_id=task_id), status_code=303)

    tr.enabled = int(enabled)
    tr.holiday_policy = holiday_policy

    new_type = trigger_type.strip() or tr.trigger_type
    if new_type != tr.trigger_type:
        tr.cron_expr = None
        tr.interval_sec = None
        tr.deadline_config = None
        tr.trigger_type = new_type

    if tr.trigger_type == "cron":
        tr.cron_expr = cron_expr.strip()
    elif tr.trigger_type == "interval":
        try:
            tr.interval_sec = int(interval_sec)
        except Exception:
            pass
    elif tr.trigger_type == "deadline":
        try:
            cfg = json.loads(tr.deadline_config or "{}")
        except Exception:
            cfg = {}
        if deadline_at.strip():
            cfg["deadline_at"] = deadline_at.strip()
        if start_before_days.strip():
            cfg["start_before_days"] = int(start_before_days)
        if interval_hours.strip():
            cfg["interval_hours"] = int(interval_hours)
        tr.deadline_config = json.dumps(cfg, ensure_ascii=False)
    db.commit()
    scheduler._reload_jobs()
    return RedirectResponse(request.url_for("task_detail", task_id=task_id), status_code=303)

@app.post("/triggers/{trigger_id}/delete")
def trigger_delete(
    request: Request,
    trigger_id: int,
    task_id: int = Form(...),
    db: Session = Depends(get_db),
):
    redir = _guard(request)
    if redir:
        return redir
    tr = db.get(Trigger, trigger_id)
    if tr:
        db.delete(tr)
        db.commit()
        scheduler._reload_jobs()
    return RedirectResponse(request.url_for("task_detail", task_id=task_id), status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=False)
