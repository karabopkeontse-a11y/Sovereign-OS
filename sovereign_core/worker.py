"""Independent durable mission worker supervisor.

The supervisor executes only through an explicitly supplied executor callable.
Run with: python -m sovereign_core.worker --executor package.module:callable
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import signal
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .store import TaskStore


class MissionExecutionError(Exception):
    """Executor failure with an explicit retryability decision."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class MissionExecutionContext:
    workspace_id: str
    mission_id: str
    worker_id: str
    shutdown_event: threading.Event
    lease_lost_event: threading.Event
    heartbeat_event: threading.Event = field(default_factory=threading.Event)

    @property
    def shutdown_requested(self) -> bool:
        return self.shutdown_event.is_set()

    @property
    def lease_lost(self) -> bool:
        return self.lease_lost_event.is_set()

    def check_cancelled(self) -> None:
        if self.lease_lost:
            raise MissionExecutionError("Mission lease was lost", retryable=False)
        if self.shutdown_requested:
            raise MissionExecutionError("Worker shutdown requested", retryable=True)


Executor = Callable[[dict, MissionExecutionContext], dict | None]


class MissionWorkerSupervisor:
    """Claim, execute, heartbeat and durably record mission outcomes."""

    def __init__(
        self,
        store: TaskStore,
        executor: Executor,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        heartbeat_interval_seconds: float | None = None,
        poll_interval_seconds: float = 2.0,
        batch_limit: int = 5,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 900.0,
        stop_event: threading.Event | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(executor):
            raise ValueError("An explicit mission executor callable is required")
        if not 10 <= int(lease_seconds) <= 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        interval = heartbeat_interval_seconds or max(1.0, lease_seconds / 3)
        if interval <= 0 or interval >= lease_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if not 1 <= int(batch_limit) <= 200:
            raise ValueError("batch_limit must be between 1 and 200")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("retry backoff bounds are invalid")
        self.store = store
        self.executor = executor
        self.worker_id = (worker_id or ("worker-" + uuid.uuid4().hex)).strip()
        if not self.worker_id:
            raise ValueError("worker_id is required")
        self.lease_seconds = int(lease_seconds)
        self.heartbeat_interval_seconds = float(interval)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.batch_limit = int(batch_limit)
        self.retry_base_seconds = float(retry_base_seconds)
        self.retry_max_seconds = float(retry_max_seconds)
        self.stop_event = stop_event or threading.Event()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._health = {
            "state": "starting", "cycles": 0, "claimed": 0,
            "completed": 0, "retryable": 0, "failed": 0,
            "lease_losses": 0, "heartbeat_failures": 0,
            "last_cycle_at": None, "last_error": None,
        }

    def health(self) -> dict:
        with self._lock:
            return dict(self._health)

    def _increment(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._health[key] += amount

    def _heartbeat_loop(
        self, job: dict, context: MissionExecutionContext, finished: threading.Event
    ) -> None:
        while not finished.wait(self.heartbeat_interval_seconds):
            if self.stop_event.is_set():
                # Continue heartbeating while the executor winds down.
                pass
            try:
                self.store.heartbeat_mission_job(
                    context.workspace_id, context.mission_id,
                    context.worker_id, lease_seconds=self.lease_seconds,
                )
                context.heartbeat_event.set()
            except Exception:
                context.lease_lost_event.set()
                self._increment("heartbeat_failures")
                return

    def _retry_delay(self, attempts: int) -> float:
        delay = self.retry_base_seconds * (2 ** max(0, attempts - 1))
        return min(self.retry_max_seconds, delay)

    def _execute(self, job: dict) -> None:
        context = MissionExecutionContext(
            workspace_id=job["workspace_id"],
            mission_id=job["id"],
            worker_id=self.worker_id,
            shutdown_event=self.stop_event,
            lease_lost_event=threading.Event(),
        )
        finished = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, context, finished),
            name="mission-heartbeat-" + job["id"][:8],
            daemon=True,
        )
        heartbeat.start()
        try:
            result = self.executor(job, context)
            finished.set()
            heartbeat.join()
            if context.lease_lost:
                self._increment("lease_losses")
                return
            if result is None:
                result = {}
            if not isinstance(result, dict):
                raise MissionExecutionError(
                    "Mission executor must return a dictionary or None",
                    retryable=False,
                )
            # Fail before writing terminal state if the result cannot be persisted.
            json.dumps(result, sort_keys=True)
            self.store.set_mission_job_status(
                job["workspace_id"], job["id"], "completed",
                worker_id=self.worker_id, result=result,
                detail="Mission executor completed",
            )
            self._increment("completed")
        except Exception as exc:
            finished.set()
            heartbeat.join()
            if context.lease_lost:
                self._increment("lease_losses")
                return
            retryable = not isinstance(exc, MissionExecutionError) or exc.retryable
            detail = (f"{type(exc).__name__}: {exc}")[:2000]
            if retryable and int(job["attempts"]) < int(job["max_attempts"]):
                retry_at = (self.clock() + timedelta(
                    seconds=self._retry_delay(int(job["attempts"]))
                )).astimezone(timezone.utc).isoformat()
                self.store.set_mission_job_status(
                    job["workspace_id"], job["id"], "retryable",
                    worker_id=self.worker_id, detail=detail, retry_at=retry_at,
                )
                self._increment("retryable")
            else:
                self.store.set_mission_job_status(
                    job["workspace_id"], job["id"], "failed",
                    worker_id=self.worker_id, detail=detail,
                )
                self._increment("failed")
        finally:
            finished.set()
            heartbeat.join(timeout=max(1.0, self.heartbeat_interval_seconds + 0.5))

    def run_once(self) -> dict:
        claimed = 0
        for workspace in self.store.list_workspaces():
            for _ in range(self.batch_limit):
                if self.stop_event.is_set():
                    break
                job = self.store.claim_mission_job(
                    workspace.id, self.worker_id, lease_seconds=self.lease_seconds
                )
                if not job:
                    break
                claimed += 1
                self._increment("claimed")
                try:
                    self._execute(job)
                except Exception as exc:
                    with self._lock:
                        self._health["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
                if self.stop_event.is_set():
                    break
        with self._lock:
            self._health["cycles"] += 1
            self._health["last_cycle_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._health["state"] = "degraded" if self._health["last_error"] else "healthy"
        return {"claimed": claimed, "health": self.health()}

    def run_forever(self, *, max_cycles: int | None = None) -> None:
        completed_cycles = 0
        with self._lock:
            self._health["state"] = "running"
        try:
            while not self.stop_event.is_set():
                self.run_once()
                completed_cycles += 1
                if max_cycles is not None and completed_cycles >= max_cycles:
                    break
                if self.stop_event.wait(self.poll_interval_seconds):
                    break
        finally:
            with self._lock:
                self._health["state"] = "stopped"


def _load_executor(spec: str) -> Executor:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("executor must use the module.path:callable format")
    target: Any = importlib.import_module(module_name)
    for part in attribute.split("."):
        target = getattr(target, part)
    if not callable(target):
        raise ValueError("Configured mission executor is not callable")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the independent SovereignOS durable mission worker."
    )
    parser.add_argument("--db", default=os.environ.get(
        "SOVEREIGN_DB_PATH", "sovereign_workspace/sovereign_tasks.sqlite3"
    ))
    parser.add_argument("--executor", required=True,
                        help="Explicit executor as package.module:callable.")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--batch-limit", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("sovereign.worker")
    try:
        executor = _load_executor(args.executor)
        store = TaskStore(Path(args.db))
        stop_event = threading.Event()
        supervisor = MissionWorkerSupervisor(
            store, executor, worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            poll_interval_seconds=args.poll_seconds,
            batch_limit=args.batch_limit, stop_event=stop_event,
        )
        def request_shutdown(signum, _frame):
            logger.info("Received shutdown signal %s", signum)
            stop_event.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, request_shutdown)
        logger.info("Mission worker starting; database=%s worker=%s",
                    args.db, supervisor.worker_id)
        supervisor.run_forever()
        logger.info("Mission worker stopped; health=%s", supervisor.health())
        return 0
    except Exception:
        logger.exception("Mission worker could not start or stopped unexpectedly")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
