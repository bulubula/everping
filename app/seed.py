import json
from sqlalchemy.orm import Session
from app.db import Base, engine, SessionLocal
from app.models import Task, Trigger

def main():
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        if db.query(Task).count() > 0:
            print("DB already seeded.")
            return

        # 1) 调度类示例：每 30 秒 echo
        t1 = Task(
            name="hello_schedule",
            type="schedule",
            command_template="echo 'hello schedule'; exit 0",
            timeout_sec_default=10,
            enabled=1,
            alert_script=None,
        )
        db.add(t1)
        db.commit()
        db.refresh(t1)
        db.add(Trigger(task_id=t1.id, trigger_type="interval", interval_sec=30, holiday_policy="NONE", enabled=1))

        # 2) 监控类示例：输出 cpu 伪数据（单值也可）
        t2 = Task(
            name="demo_monitor",
            type="monitor",
            command_template="echo -e \"OUT=cpu=23.5\\ttemp=67.2\"; exit 0",
            timeout_sec_default=5,
            enabled=1,
            alert_script=None,
        )
        db.add(t2)
        db.commit()
        db.refresh(t2)
        db.add(Trigger(task_id=t2.id, trigger_type="interval", interval_sec=2, holiday_policy="NONE", enabled=1))

        db.commit()
        print("Seed done. Open http://127.0.0.1:8000")

if __name__ == "__main__":
    main()
