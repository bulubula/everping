from __future__ import annotations
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Trigger
from app.services import enqueue_run, holiday_allowed

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
                # 期望 JSON: {"start_after_sec": 0, "interval_sec": 18000}
                import json
                cfg = json.loads(t.deadline_config)
                interval_sec = int(cfg.get("interval_sec", 3600))
                trig = IntervalTrigger(seconds=interval_sec)
                self.sched.add_job(self._fire, trig, args=[t.id], id=f"trigger_{t.id}", replace_existing=True)

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
            enqueue_run(db, task.id, t.id)
