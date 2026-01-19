from __future__ import annotations
from datetime import datetime, timedelta
import json
import os
from sqlalchemy.orm import Session
from sqlalchemy import select, update
from app.models import Task, Trigger, Run, Metric, AlertState
from app.utils import now_utc, ensure_dir, parse_out_line, parse_metrics_tokens
from app.config import settings
from app.executor import run_command_killpg

def enqueue_run(db: Session, task_id: int, trigger_id: int | None) -> int:
    r = Run(task_id=task_id, trigger_id=trigger_id, status="PENDING", scheduled_at=now_utc())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r.id

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
        # 没装库/异常：默认放行
        return True

def acquire_task_mutex(db: Session, task_id: int) -> bool:
    """
    父任务不可重入：如果该 task_id 已存在 RUNNING，则获取失败
    """
    q = select(Run.id).where(Run.task_id == task_id, Run.status == "RUNNING").limit(1)
    exists = db.execute(q).scalar_one_or_none()
    return exists is None

def write_run_logs(run_id: int, stdout: str, stderr: str) -> tuple[str, str]:
    ensure_dir(settings.LOG_DIR)
    out_path = os.path.join(settings.LOG_DIR, f"run_{run_id}.out.log")
    err_path = os.path.join(settings.LOG_DIR, f"run_{run_id}.err.log")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(stdout or "")
    with open(err_path, "w", encoding="utf-8") as f:
        f.write(stderr or "")
    return out_path, err_path

def maybe_send_alert(db: Session, task: Task, alert_type: str, message: str) -> bool:
    """
    抑制：task_id + alert_type
    """
    now = now_utc()
    st = db.execute(
        select(AlertState).where(AlertState.task_id == task.id, AlertState.alert_type == alert_type)
    ).scalar_one_or_none()

    if st and st.last_sent_at and (now - st.last_sent_at).total_seconds() < settings.ALERT_SUPPRESS_SEC:
        return False

    if not st:
        st = AlertState(task_id=task.id, alert_type=alert_type, last_sent_at=now)
        db.add(st)
    else:
        st.last_sent_at = now

    db.commit()

    # 触发你自己的通知脚本（可选）
    if task.alert_script:
        # 不要阻塞：简单用 shell 后台执行
        os.system(f"nohup {task.alert_script} '{message.replace(\"'\", \"\\\"\")}' >/dev/null 2>&1 &")
    return True

def execute_one_run(db: Session, run_id: int) -> None:
    run = db.get(Run, run_id)
    if not run or run.status != "PENDING":
        return

    task = db.get(Task, run.task_id)
    if not task or task.enabled != 1:
        run.status = "SKIPPED"
        run.finished_at = now_utc()
        db.commit()
        return

    # 互斥：父任务不可重入
    if not acquire_task_mutex(db, task.id):
        run.status = "FAILED"
        run.started_at = now_utc()
        run.finished_at = now_utc()
        run.exit_code = 99
        run.error_message = "Task is already RUNNING (non-reentrant)."
        db.commit()
        maybe_send_alert(db, task, "reentry", f"{task.name}: reentry blocked")
        return

    run.status = "RUNNING"
    run.started_at = now_utc()
    db.commit()

    try:
        if task.type == "workflow":
            _execute_workflow(db, run, task)
        else:
            _execute_single(db, run, task, [])
    except Exception as e:
        run.status = "FAILED"
        run.finished_at = now_utc()
        run.exit_code = 98
        run.error_message = f"Internal error: {e}"
        db.commit()
        maybe_send_alert(db, task, "internal_error", f"{task.name}: internal error: {e}")

def _execute_single(db: Session, run: Run, task: Task, args: list[str]) -> tuple[int, list[str], str, str, bool]:
    cmd = task.command_template.strip()
    if args:
        # 追加参数，注意简单转义：这里用最小实现（空格与引号可能需要你在脚本里自己处理）
        cmd = cmd + " " + " ".join([_sh_quote(a) for a in args])

    res = run_command_killpg(cmd, timeout_sec=task.timeout_sec_default)

    out_path, err_path = write_run_logs(run.id, res.stdout, res.stderr)
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
    # 监控类写 metrics
    if task.type == "monitor" and tokens:
        for k, v in parse_metrics_tokens(tokens):
            db.add(Metric(task_id=task.id, key=k, value=v))
        db.commit()

    if run.status in ("FAILED", "TIMEOUT"):
        maybe_send_alert(db, task, "exec_failed", f"{task.name}: status={run.status} code={run.exit_code}")

    return res.exit_code, tokens, res.stdout, res.stderr, res.timed_out

def _execute_workflow(db: Session, run: Run, task: Task) -> None:
    """
    workflow_def JSON 示例：
    {
      "steps": [
        {"id": 1, "cmd": "echo -e 'OUT=hello\\tworld'", "timeout": 10, "on_success": 2, "on_fail": 0},
        {"id": 2, "cmd": "echo \"args: $1 $2\"; exit 0", "timeout": 10, "on_success": 0, "on_fail": 0}
      ],
      "entry": 1
    }
    """
    wf = json.loads(task.workflow_def or "{}")
    steps = {s["id"]: s for s in wf.get("steps", [])}
    cur = wf.get("entry", 0)
    args: list[str] = []

    while cur and cur in steps:
        s = steps[cur]
        step_cmd = s.get("cmd", "")
        step_timeout = int(s.get("timeout", task.timeout_sec_default))

        # step 也复用同一个 run 的日志文件（简单起见）；也可扩展为子 run
        step_task = Task(
            id=task.id,
            name=task.name,
            type="workflow",
            command_template=step_cmd,
            enabled=1,
            timeout_sec_default=step_timeout,
            workflow_def=None,
            alert_script=task.alert_script,
        )

        code, out_tokens, _, _, timed_out = _execute_single(db, run, step_task, args)
        ok = (code == 0) and (not timed_out)

        # OUT 作为下一步参数
        if out_tokens:
            args = out_tokens

        cur = int(s.get("on_success", 0) if ok else s.get("on_fail", 0))

    # workflow 最终状态以 run 当前状态为准：如果最后一步成功则 SUCCESS，否则 FAILED/TIMEOUT
    # 若 workflow 没有 step 或没跑起来：
    if run.status == "RUNNING":
        run.status = "SUCCESS"
        run.exit_code = 0
        run.finished_at = now_utc()
        db.commit()

def _sh_quote(s: str) -> str:
    # 最小安全：单引号包裹；内部单引号替换
    return "'" + s.replace("'", "'\"'\"'") + "'"
