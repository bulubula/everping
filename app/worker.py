from __future__ import annotations
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Run
from app.services import execute_one_run
from app.config import settings

class WorkerPool:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=settings.MAX_WORKERS)
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with SessionLocal() as db:
                    # 取一批 pending（简单实现：一次最多 MAX_WORKERS）
                    q = select(Run.id).where(Run.status == "PENDING").order_by(Run.scheduled_at).limit(settings.MAX_WORKERS)
                    run_ids = [r for r in db.execute(q).scalars().all()]

                for rid in run_ids:
                    self._executor.submit(self._run_one, rid)

            except Exception:
                pass

            time.sleep(0.5)

    def _run_one(self, run_id: int) -> None:
        with SessionLocal() as db:
            execute_one_run(db, run_id)
