from __future__ import annotations
from fastapi import Request
from passlib.context import CryptContext
from app.config import settings

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_login(username: str, password: str) -> bool:
    return username == settings.ADMIN_USER and password == settings.ADMIN_PASS

def require_login(request: Request) -> bool:
    return bool(request.session.get("user"))

def login(request: Request, username: str) -> None:
    request.session["user"] = username

def logout(request: Request) -> None:
    request.session.pop("user", None)
