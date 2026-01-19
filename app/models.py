from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, Float, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    type: Mapped[str] = mapped_column(String(20))  # schedule | monitor | workflow
    command_template: Mapped[str] = mapped_column(Text)  # bash command template
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    timeout_sec_default: Mapped[int] = mapped_column(Integer, default=60)
    workflow_def: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON text (only for workflow)
    alert_script: Mapped[str | None] = mapped_column(Text, nullable=True)  # optional bash cmd
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    triggers: Mapped[list["Trigger"]] = relationship(back_populates="task", cascade="all, delete-orphan")

class Trigger(Base):
    __tablename__ = "triggers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    trigger_type: Mapped[str] = mapped_column(String(20))  # cron | interval | deadline | once
    cron_expr: Mapped[str | None] = mapped_column(String(120), nullable=True)  # "*/5 * * * *"
    interval_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline_config: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON text
    holiday_policy: Mapped[str] = mapped_column(String(30), default="NONE")
    enabled: Mapped[int] = mapped_column(Integer, default=1)

    task: Mapped["Task"] = relationship(back_populates="triggers")

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    trigger_id: Mapped[int | None] = mapped_column(ForeignKey("triggers.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)  # PENDING RUNNING SUCCESS FAILED TIMEOUT SKIPPED
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    stderr_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

class Metric(Base):
    __tablename__ = "metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    key: Mapped[str] = mapped_column(String(80), default="value")
    value: Mapped[float] = mapped_column(Float)

class AlertState(Base):
    __tablename__ = "alert_state"
    __table_args__ = (UniqueConstraint("task_id", "alert_type", name="uq_alert_state"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    alert_type: Mapped[str] = mapped_column(String(80))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
