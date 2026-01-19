from pydantic import BaseModel
from typing import Optional

class TaskCreate(BaseModel):
    name: str
    type: str  # schedule|monitor|workflow
    command_template: str
    timeout_sec_default: int = 60
    workflow_def: Optional[str] = None
    alert_script: Optional[str] = None
    enabled: int = 1

class TriggerCreate(BaseModel):
    trigger_type: str  # cron|interval|deadline
    cron_expr: Optional[str] = None
    interval_sec: Optional[int] = None
    deadline_config: Optional[str] = None
    holiday_policy: str = "NONE"
    enabled: int = 1
