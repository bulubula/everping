from __future__ import annotations
import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Optional

@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

def run_command_killpg(command: str, timeout_sec: int, term_grace_sec: int = 5) -> ExecResult:
    """
    以新进程组启动；超时：TERM -> 等待 -> KILL；确保杀进程树（同进程组）。
    """
    p = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=os.setsid if os.name != "nt" else None,  # new process group (posix)
        executable="/bin/bash" if os.name != "nt" else None,
    )

    try:
        out, err = p.communicate(timeout=timeout_sec)
        return ExecResult(exit_code=p.returncode or 0, stdout=out, stderr=err, timed_out=False)
    except subprocess.TimeoutExpired:
        # TERM
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass

        try:
            out, err = p.communicate(timeout=term_grace_sec)
            # 仍可能返回非0，交给上层
            return ExecResult(exit_code=p.returncode or 124, stdout=out, stderr=err, timed_out=True)
        except subprocess.TimeoutExpired:
            # KILL
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
            out, err = p.communicate()
            return ExecResult(exit_code=137, stdout=out, stderr=err, timed_out=True)

def run_argv_killpg(argv: list[str], timeout_sec: int, term_grace_sec: int = 5) -> ExecResult:
    p = subprocess.Popen(
        argv,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=os.setsid if os.name != "nt" else None,  # new process group (posix)
    )

    try:
        out, err = p.communicate(timeout=timeout_sec)
        return ExecResult(exit_code=p.returncode or 0, stdout=out, stderr=err, timed_out=False)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass

        try:
            out, err = p.communicate(timeout=term_grace_sec)
            return ExecResult(exit_code=p.returncode or 124, stdout=out, stderr=err, timed_out=True)
        except subprocess.TimeoutExpired:
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
            out, err = p.communicate()
            return ExecResult(exit_code=137, stdout=out, stderr=err, timed_out=True)
