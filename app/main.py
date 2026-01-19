from __future__ import annotations
import json
from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.config import settings
from app.db import Base, engine, get_db
from app.models import Task, Trigger, Run, Metric
from app.auth import verify_login, require_login, login, logout
from app.scheduler import AppScheduler
from app.worker import WorkerPool
from app.services import enqueue_run

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=settings.APP_SECRET)
templates = Jinja2Templates(directory="app/templates")

scheduler = AppScheduler()
workers = WorkerPool()

@app.on_event("startup")
def _startup():
    scheduler.start()
    workers.start()

@app.on_event("shutdown")
def _shutdown():
    scheduler.shutdown()
    workers.stop()

def _guard(request: Request):
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    redir = _guard(request)
    if redir:
        return redir
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login")
def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_login(username, password):
        login(request, username)
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})

@app.post("/logout")
def do_logout(request: Request):
    logout(request)
    return RedirectResponse("/login", status_code=303)

@app.get("/tasks", response_class=HTMLResponse)
def tasks(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    items = db.execute(select(Task).order_by(Task.id.desc())).scalars().all()
    return templates.TemplateResponse("tasks.html", {"request": request, "tasks": items})

@app.get("/tasks/new", response_class=HTMLResponse)
def task_new(request: Request):
    redir = _guard(request)
    if redir:
        return redir
    return templates.TemplateResponse("task_new.html", {"request": request})

@app.post("/tasks/new")
def task_new_post(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    command_template: str = Form(...),
    timeout_sec_default: int = Form(60),
    workflow_def: str = Form(""),
    alert_script: str = Form(""),
    enabled: int = Form(1),
    db: Session = Depends(get_db),
):
    redir = _guard(request)
    if redir:
        return redir

    wf = workflow_def.strip() or None
    al = alert_script.strip() or None
    t = Task(
        name=name.strip(),
        type=type.strip(),
        command_template=command_template.strip(),
        timeout_sec_default=int(timeout_sec_default),
        workflow_def=wf,
        alert_script=al,
        enabled=int(enabled),
    )
    db.add(t)
    db.commit()
    return RedirectResponse("/tasks", status_code=303)

@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: int, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    t = db.get(Task, task_id)
    trigs = db.execute(select(Trigger).where(Trigger.task_id == task_id).order_by(Trigger.id.desc())).scalars().all()
    return templates.TemplateResponse("task_detail.html", {"request": request, "task": t, "triggers": trigs})

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
    scheduler.start()  # ensure running
    # 重新加载 job（最小实现：直接重启/重载）
    scheduler._reload_jobs()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)

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
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)

@app.post("/tasks/{task_id}/run")
def run_now(request: Request, task_id: int, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    enqueue_run(db, task_id, None)
    return RedirectResponse("/runs", status_code=303)

@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    items = db.execute(select(Run).order_by(Run.id.desc()).limit(200)).scalars().all()
    tasks = {t.id: t for t in db.execute(select(Task)).scalars().all()}
    return templates.TemplateResponse("runs.html", {"request": request, "runs": items, "tasks": tasks})

@app.get("/metrics", response_class=HTMLResponse)
def metrics(request: Request, db: Session = Depends(get_db)):
    redir = _guard(request)
    if redir:
        return redir
    # 最简单：最近 300 条
    items = db.execute(select(Metric).order_by(Metric.id.desc()).limit(300)).scalars().all()
    tasks = {t.id: t for t in db.execute(select(Task)).scalars().all()}
    return templates.TemplateResponse("metrics.html", {"request": request, "metrics": items, "tasks": tasks})
