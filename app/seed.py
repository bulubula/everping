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

        # 3) workflow 示例：A 输出两个参数给 B
        wf = {
            "entry": 1,
            "steps": [
                {"id": 1, "cmd": "echo -e 'OUT=arg1\\targ2'; exit 0", "timeout": 5, "on_success": 2, "on_fail": 0},
                {"id": 2, "cmd": "echo \"step2 got: $1 $2\"; exit 0", "timeout": 5, "on_success": 0, "on_fail": 0},
            ],
        }
        t3 = Task(
            name="demo_workflow",
            type="workflow",
            command_template="(workflow entry)",  # 不用
            workflow_def=json.dumps(wf),
            timeout_sec_default=10,
            enabled=1,
            alert_script=None,
        )
        db.add(t3)
        db.commit()
        db.refresh(t3)

        db.commit()
        print("Seed done. Open http://127.0.0.1:8000")

if __name__ == "__main__":
    main()
