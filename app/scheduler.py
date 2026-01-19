from __future__ import annotations
import json
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Trigger
from datetime import datetime, timedelta
from app.services import enqueue_run, holiday_allowed
from app.utils import now_utc

class AppScheduler:
    def __init__(self) -> None:
        self.sched = BackgroundScheduler(timezone="Asia/Shanghai")
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
            if t.trigger_type == "once":
                self._fire_once(t.id)
                continue
            if t.trigger_type == "cron" and t.cron_expr:
                # 5段 cron：min hour dom mon dow
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
                # 最小实现：deadline 先用 interval，复杂计算你后续再增强
                # 期望 JSON: {"deadline_at": "...", "start_before_days": 1, "interval_hours": 6}
                import json
                try:
                    cfg = json.loads(t.deadline_config)
                    interval_hours = int(cfg.get("interval_hours", 6))
                except Exception:
                    interval_hours = 6
                trig = IntervalTrigger(seconds=max(interval_hours, 1) * 3600)
                self.sched.add_job(
                    self._fire, trig, args=[t.id], id=f"trigger_{t.id}", replace_existing=True
                )

    def _fire_once(self, trigger_id: int) -> None:
        with SessionLocal() as db:
            t = db.get(Trigger, trigger_id)
            if not t or t.enabled != 1:
                return
            task = t.task
            if not task or task.enabled != 1:
                t.enabled = 0
                db.commit()
                return
            if not holiday_allowed(t.holiday_policy):
                t.enabled = 0
                db.commit()
                return
            enqueue_run(db, task.id, t.id)
            t.enabled = 0
            db.commit()

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
                try:
                    deadline_dt = datetime.fromisoformat(deadline_at)
                except ValueError:
                    return
                start_dt = deadline_dt - timedelta(days=start_before_days)
                now = now_utc()
                if now < start_dt:
                    return
                if now > deadline_dt:
                    t.enabled = 0
                    db.commit()
                    return
            enqueue_run(db, task.id, t.id)
