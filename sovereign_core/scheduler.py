"""Independent supervisor for promoting durable mission schedules.

Run with: python -m sovereign_core.scheduler
The supervisor only promotes schedules into the durable mission queue. It
never claims or executes mission jobs; workers remain a separate service.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .store import TaskStore


class MissionSchedulerSupervisor:
    """Poll all workspaces and promote due schedules independently of API calls."""

    def __init__(
        self,
        store: TaskStore,
        *,
        poll_interval_seconds: float = 5.0,
        batch_limit: int = 50,
        clock: Callable[[], datetime] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if not 1 <= int(batch_limit) <= 200:
            raise ValueError("batch_limit must be between 1 and 200")
        self.store = store
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.batch_limit = int(batch_limit)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stop_event = stop_event or threading.Event()
        self._lock = threading.Lock()
        self._health = {
            "state": "starting",
            "cycles": 0,
            "successful_cycles": 0,
            "failed_cycles": 0,
            "last_cycle_at": None,
            "last_success_at": None,
            "last_error": None,
            "workspaces_seen": 0,
            "one_time_promoted": 0,
            "recurring_promoted": 0,
        }

    def health(self) -> dict:
        """Return a snapshot without exposing schedule payloads or credentials."""
        with self._lock:
            return dict(self._health)

    def run_once(self) -> dict:
        """Run one bounded promotion cycle across all current workspaces."""
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        now_utc = now.astimezone(timezone.utc).isoformat()
        workspaces = self.store.list_workspaces()
        one_time_count = 0
        recurring_count = 0
        errors: list[dict[str, str]] = []

        for workspace in workspaces:
            workspace_id = workspace.id
            for schedule_kind, promote in (
                ("one_time", self.store.promote_due_mission_schedules),
                ("recurring", self.store.promote_due_recurring_mission_schedules),
            ):
                try:
                    promoted = promote(
                        workspace_id, limit=self.batch_limit, now=now_utc
                    )
                    if schedule_kind == "one_time":
                        one_time_count += len(promoted)
                    else:
                        recurring_count += len(promoted)
                except Exception as exc:
                    # Isolate failures: another schedule type/workspace can still progress.
                    errors.append({
                        "workspace_id": str(workspace_id),
                        "schedule_kind": schedule_kind,
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    })

        stamp = datetime.now(timezone.utc).isoformat()
        succeeded = not errors
        with self._lock:
            self._health["cycles"] += 1
            self._health["workspaces_seen"] = len(workspaces)
            self._health["one_time_promoted"] += one_time_count
            self._health["recurring_promoted"] += recurring_count
            self._health["last_cycle_at"] = stamp
            if succeeded:
                self._health["successful_cycles"] += 1
                self._health["last_success_at"] = stamp
                self._health["last_error"] = None
                self._health["state"] = "healthy"
            else:
                self._health["failed_cycles"] += 1
                self._health["last_error"] = errors
                self._health["state"] = "degraded"

        return {
            "workspaces_seen": len(workspaces),
            "one_time_promoted": one_time_count,
            "recurring_promoted": recurring_count,
            "errors": errors,
        }

    def run_forever(self, *, max_cycles: int | None = None) -> None:
        """Poll until stopped; max_cycles exists for deterministic tests/operations."""
        completed = 0
        with self._lock:
            self._health["state"] = "running"
        try:
            while not self.stop_event.is_set():
                self.run_once()
                completed += 1
                if max_cycles is not None and completed >= max_cycles:
                    break
                if self.stop_event.wait(self.poll_interval_seconds):
                    break
        finally:
            with self._lock:
                self._health["state"] = (
                    "stopped" if self._health["state"] != "degraded" else "degraded"
                )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the independent SovereignOS mission schedule supervisor."
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "SOVEREIGN_DB_PATH", "sovereign_workspace/sovereign_tasks.sqlite3"
        ),
        help="SQLite database path (or set SOVEREIGN_DB_PATH).",
    )
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--batch-limit", type=int, default=50)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("sovereign.scheduler")
    store = TaskStore(Path(args.db))
    stop_event = threading.Event()
    supervisor = MissionSchedulerSupervisor(
        store,
        poll_interval_seconds=args.poll_seconds,
        batch_limit=args.batch_limit,
        stop_event=stop_event,
    )

    def request_shutdown(signum, _frame):
        logger.info("Received shutdown signal %s", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_shutdown)

    logger.info("Mission scheduler supervisor starting; database=%s", args.db)
    try:
        supervisor.run_forever()
    except Exception:
        logger.exception("Mission scheduler supervisor stopped unexpectedly")
        return 1
    logger.info("Mission scheduler supervisor stopped; health=%s", supervisor.health())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
