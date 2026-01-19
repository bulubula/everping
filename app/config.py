import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    APP_SECRET: str = os.getenv("APP_SECRET", "change_me")
    ADMIN_USER: str = os.getenv("ADMIN_USER", "admin")
    ADMIN_PASS: str = os.getenv("ADMIN_PASS", "admin123")
    DB_URL: str = os.getenv("DB_URL", "sqlite:///./data/app.db")
    MAX_WORKERS: int = int(os.getenv("MAX_WORKERS", "8"))
    ALERT_SUPPRESS_SEC: int = int(os.getenv("ALERT_SUPPRESS_SEC", "900"))
    LOG_DIR: str = os.getenv("LOG_DIR", "./data/logs")

settings = Settings()