import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    APP_SECRET: str = os.getenv("APP_SECRET", "change_me")
    ADMIN_USER: str = os.getenv("ADMIN_USER", "admin")
    ADMIN_PASS: str = os.getenv("ADMIN_PASS", "admin123")
    DB_URL: str = os.getenv("DB_URL", "sqlite:///./data/app.db")
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8000"))
    ROOT_PATH: str = os.getenv("ROOT_PATH", "")
    MAX_WORKERS: int = int(os.getenv("MAX_WORKERS", "8"))
    ALERT_SUPPRESS_SEC: int = int(os.getenv("ALERT_SUPPRESS_SEC", "900"))
    LOG_DIR: str = os.getenv("LOG_DIR", "./data/logs")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", "10485760"))
    LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "5"))
    APP_LOG_NAME: str = os.getenv("APP_LOG_NAME", "app.log")
    METRICS_RETENTION_DAYS: int = int(os.getenv("METRICS_RETENTION_DAYS", "30"))
    METRICS_DIR: str = os.getenv("METRICS_DIR", "./data/metrics")
    ALERT_PUSH_SCRIPT: str = os.getenv("ALERT_PUSH_SCRIPT", "/root/sh/push.py")
    ALERT_PUSH_TITLE: str = os.getenv("ALERT_PUSH_TITLE", "everping")
    ALERT_PUSH_GROUP: str = os.getenv("ALERT_PUSH_GROUP", "WH-ubuntu")
    ALERT_PUSH_LEVEL: str = os.getenv("ALERT_PUSH_LEVEL", "active")
    RUN_ZOMBIE_SEC: int = int(os.getenv("RUN_ZOMBIE_SEC", "3600"))
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Shanghai")
    JOBS_FILE: str = os.getenv("JOBS_FILE", "./jobs.json")
    DEFAULT_TIMEOUT_SEC: int = int(os.getenv("DEFAULT_TIMEOUT_SEC", "60"))

settings = Settings()
