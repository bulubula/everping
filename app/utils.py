from __future__ import annotations
from datetime import datetime
from typing import List, Tuple
import os

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def now_utc() -> datetime:
    return datetime.utcnow()

def parse_out_line(stdout_text: str) -> list[str]:
    """
    找最后一个 OUT= 行，按 \\t split
    """
    lines = [ln.rstrip("\n") for ln in stdout_text.splitlines()]
    out_lines = [ln for ln in lines if ln.startswith("OUT=")]
    if not out_lines:
        return []
    payload = out_lines[-1][4:]
    if payload == "":
        return []
    return payload.split("\t")

def parse_metrics_tokens(tokens: list[str]) -> list[tuple[str, float]]:
    """
    token 可能是 "23.5" 或 "cpu=23.5"
    """
    pairs: list[tuple[str, float]] = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        if "=" in t:
            k, v = t.split("=", 1)
            try:
                pairs.append((k.strip(), float(v.strip())))
            except ValueError:
                continue
        else:
            try:
                pairs.append(("value", float(t)))
            except ValueError:
                continue
    return pairs
