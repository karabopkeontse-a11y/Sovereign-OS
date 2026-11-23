"""Independent mission scheduler supervisor tests."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from sovereign_core.scheduler import MissionSchedulerSupervisor
from sovereign_core.store import TaskStore


class MissionSchedulerSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "scheduler.sqlite3")
        self.workspace = self.store.create_workspace("Scheduler A", "Tests")
        self.other_workspace = self.store.create_workspace("Scheduler B", "Isolation")
        self.now = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def test_run_once_promotes_both_schedule_types_across_workspaces(self):
        self.store.create_mission_schedule(
            self.workspace.id, "One time", {"kind": "once"},
            "2026-11-24T08:00:00+00:00", "UTC",
            idempotency_key="one-time",
        )
        self.store.create_recurring_mission_schedule(
            self.other_workspace.id, "Daily", {"kind": "daily"},
            "daily", "08:00", "UTC", "2026-11-24",
            idempotency_key="daily",
        )
        supervisor = MissionSchedulerSupervisor(
            self.store, clock=lambda: self.now
        )

        result = supervisor.run_once()

        self.assertEqual(result["workspaces_seen"], 2)
        self.assertEqual(result["one_time_promoted"], 1)
        self.assertEqual(result["recurring_promoted"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(supervisor.health()["state"], "healthy")
        self.assertEqual(supervisor.health()["successful_cycles"], 1)
        self.assertEqual(
            len(self.store.list_mission_jobs(self.workspace.id)), 1
        )
        self.assertEqual(
            len(self.store.list_mission_jobs(self.other_workspace.id)), 1
        )

    def test_cycle_is_idempotent_after_restart_or_repeated_poll(self):
        self.store.create_mission_schedule(
            self.workspace.id, "One time", {},
            "2026-11-24T08:00:00+00:00", "UTC",
            idempotency_key="one-time",
        )
        first = MissionSchedulerSupervisor(self.store, clock=lambda: self.now)
        second = MissionSchedulerSupervisor(self.store, clock=lambda: self.now)

        self.assertEqual(first.run_once()["one_time_promoted"], 1)
        self.assertEqual(second.run_once()["one_time_promoted"], 0)
        self.assertEqual(len(self.store.list_mission_jobs(self.workspace.id)), 1)

    def test_failure_is_isolated_and_reported_in_health(self):
        class PartiallyFailingStore:
            def list_workspaces(self):
                return [SimpleNamespace(id="workspace-a"), SimpleNamespace(id="workspace-b")]

            def promote_due_mission_schedules(self, workspace_id, **kwargs):
                if workspace_id == "workspace-a":
                    raise RuntimeError("database temporarily busy")
                return [{"id": "job-1"}]

            def promote_due_recurring_mission_schedules(self, workspace_id, **kwargs):
                return []

        supervisor = MissionSchedulerSupervisor(
            PartiallyFailingStore(), clock=lambda: self.now
        )
        result = supervisor.run_once()

        self.assertEqual(result["one_time_promoted"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["workspace_id"], "workspace-a")
        self.assertEqual(supervisor.health()["state"], "degraded")
        self.assertEqual(supervisor.health()["failed_cycles"], 1)

    def test_run_forever_is_bounded_and_stops_cleanly(self):
        class NoWaitStopEvent:
            def is_set(self):
                return False

            def wait(self, _seconds):
                return False

        store = Mock()
        store.list_workspaces.return_value = []
        supervisor = MissionSchedulerSupervisor(
            store, stop_event=NoWaitStopEvent(), clock=lambda: self.now
        )

        supervisor.run_forever(max_cycles=3)

        self.assertEqual(store.list_workspaces.call_count, 3)
        self.assertEqual(supervisor.health()["cycles"], 3)
        self.assertEqual(supervisor.health()["state"], "stopped")

    def test_rejects_invalid_polling_configuration_and_naive_clock(self):
        with self.assertRaisesRegex(ValueError, "poll_interval_seconds"):
            MissionSchedulerSupervisor(self.store, poll_interval_seconds=0)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            MissionSchedulerSupervisor(
                self.store, clock=lambda: datetime(2026, 11, 24, 8, 0)
            ).run_once()


if __name__ == "__main__":
    unittest.main()
