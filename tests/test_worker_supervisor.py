"""Durable mission worker supervisor acceptance tests."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from sovereign_core.store import TaskStore
from sovereign_core.worker import (
    MissionExecutionError,
    MissionWorkerSupervisor,
)


class MissionWorkerSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "worker.sqlite3")
        self.workspace = self.store.create_workspace("Worker A", "Execution")
        self.other_workspace = self.store.create_workspace("Worker B", "Isolation")

    def tearDown(self):
        self.temp.cleanup()

    def test_executor_result_is_persisted_with_completed_event(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Create report", {"topic": "Botswana"}
        )

        def executor(claimed, context):
            self.assertEqual(claimed["id"], job["id"])
            self.assertFalse(context.lease_lost)
            return {"artifact": "reports/briefing.md", "verified": True}

        worker = MissionWorkerSupervisor(
            self.store, executor, worker_id="worker-test"
        )
        result = worker.run_once()

        saved = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(result["claimed"], 1)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(
            saved["result"],
            {"artifact": "reports/briefing.md", "verified": True},
        )
        self.assertEqual(
            [event["event_type"] for event in
             self.store.list_mission_job_events(self.workspace.id, job["id"])],
            ["created", "claimed", "completed"],
        )
        self.assertEqual(worker.health()["completed"], 1)

    def test_retryable_failure_is_durably_delayed_and_bounded(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Retry me", {}, max_attempts=3
        )
        now = datetime.now(timezone.utc)
        worker = MissionWorkerSupervisor(
            self.store,
            lambda _job, _context: (_ for _ in ()).throw(
                MissionExecutionError("temporary provider outage")
            ),
            worker_id="worker-retry",
            retry_base_seconds=30,
            retry_max_seconds=120,
            clock=lambda: now,
        )

        worker.run_once()

        saved = self.store.get_mission_job(self.workspace.id, job["id"])
        retry_at = datetime.fromisoformat(saved["scheduled_at"])
        self.assertEqual(saved["status"], "retryable")
        self.assertEqual(saved["attempts"], 1)
        self.assertEqual(retry_at, now + timedelta(seconds=30))
        self.assertIn("temporary provider outage", saved["last_error"])
        self.assertIsNone(
            self.store.claim_mission_job(self.workspace.id, "worker-too-early")
        )
        self.assertEqual(worker.health()["retryable"], 1)

    def test_nonretryable_failure_is_terminal(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Invalid mission", {}, max_attempts=4
        )
        worker = MissionWorkerSupervisor(
            self.store,
            lambda _job, _context: (_ for _ in ()).throw(
                MissionExecutionError("invalid mission payload", retryable=False)
            ),
            worker_id="worker-permanent",
        )

        worker.run_once()

        saved = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertIn("invalid mission payload", saved["last_error"])
        self.assertEqual(worker.health()["failed"], 1)

    def test_attempt_budget_exhaustion_marks_job_failed(self):
        job = self.store.create_mission_job(
            self.workspace.id, "One attempt only", {}, max_attempts=1
        )
        worker = MissionWorkerSupervisor(
            self.store,
            lambda _job, _context: (_ for _ in ()).throw(
                RuntimeError("executor crashed")
            ),
            worker_id="worker-exhausted",
        )

        worker.run_once()

        saved = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["attempts"], 1)

    def test_heartbeat_keeps_long_running_execution_lease_alive(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Long task", {}, max_attempts=2
        )
        observed = {}

        def executor(_job, context):
            time.sleep(0.08)
            observed["heartbeat"] = context.heartbeat_event.is_set()
            observed["lease_lost"] = context.lease_lost
            observed["heartbeat_at"] = self.store.get_mission_job(
                self.workspace.id, _job["id"]
            )["heartbeat_at"]
            return {"ok": True}

        worker = MissionWorkerSupervisor(
            self.store, executor, worker_id="worker-heartbeat",
            lease_seconds=10, heartbeat_interval_seconds=0.02,
        )
        worker.run_once()

        saved = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertTrue(observed["heartbeat"])
        self.assertFalse(observed["lease_lost"])
        self.assertTrue(observed["heartbeat_at"])
        # Terminal transitions clear the active heartbeat and lease.
        self.assertIsNone(saved["heartbeat_at"])
        self.assertEqual(saved["status"], "completed")

    def test_heartbeat_failure_prevents_stale_worker_completion(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Lease lost task", {}, max_attempts=2
        )

        def heartbeat_failure(*_args, **_kwargs):
            raise RuntimeError("simulated lease ownership loss")

        self.store.heartbeat_mission_job = heartbeat_failure

        def executor(_job, context):
            self.assertTrue(context.lease_lost_event.wait(timeout=1))
            return {"must_not_complete": True}

        worker = MissionWorkerSupervisor(
            self.store, executor, worker_id="worker-lease-lost",
            lease_seconds=10, heartbeat_interval_seconds=0.01,
        )
        worker.run_once()

        saved = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(saved["status"], "running")
        # Claim initializes an empty result; the stale executor result must not replace it.
        self.assertEqual(saved["result"], {})
        self.assertEqual(worker.health()["heartbeat_failures"], 1)
        self.assertEqual(worker.health()["lease_losses"], 1)

    def test_new_worker_reclaims_expired_lease_after_process_restart(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Recover after restart", {}, max_attempts=3
        )
        claimed = self.store.claim_mission_job(
            self.workspace.id, "worker-before-restart", lease_seconds=10
        )
        self.assertEqual(claimed["id"], job["id"])

        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.store._connection() as db:
            db.execute(
                "UPDATE mission_jobs SET lease_expires_at=? WHERE id=?",
                (expired_at, job["id"]),
            )

        restarted_store = TaskStore(Path(self.temp.name) / "worker.sqlite3")
        worker = MissionWorkerSupervisor(
            restarted_store,
            lambda _job, _context: {"recovered": True},
            worker_id="worker-after-restart",
        )
        worker.run_once()

        saved = restarted_store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["attempts"], 2)
        self.assertEqual(saved["result"], {"recovered": True})
        event_types = [
            event["event_type"] for event in
            restarted_store.list_mission_job_events(self.workspace.id, job["id"])
        ]
        self.assertIn("lease_reclaimed", event_types)
        self.assertEqual(event_types[-1], "completed")

    def test_shutdown_stops_claiming_after_current_executor_returns(self):
        self.store.create_mission_job(self.workspace.id, "First", {})
        self.store.create_mission_job(self.workspace.id, "Second", {})
        stop = Event()
        calls = []

        def executor(job, _context):
            calls.append(job["title"])
            stop.set()
            return {"finished": True}

        worker = MissionWorkerSupervisor(
            self.store, executor, worker_id="worker-shutdown",
            stop_event=stop, batch_limit=5,
        )
        worker.run_forever(max_cycles=5)

        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.health()["state"], "stopped")
        remaining = self.store.list_mission_jobs(
            self.workspace.id, statuses=["queued"]
        )
        self.assertEqual(len(remaining), 1)

    def test_worker_only_claims_jobs_in_each_workspace_scope(self):
        first = self.store.create_mission_job(self.workspace.id, "Workspace A", {})
        second = self.store.create_mission_job(self.other_workspace.id, "Workspace B", {})
        seen = []
        worker = MissionWorkerSupervisor(
            self.store,
            lambda job, _context: (seen.append((job["workspace_id"], job["id"])) or {}),
            worker_id="worker-multi",
        )

        worker.run_once()

        self.assertEqual(
            set(seen),
            {(self.workspace.id, first["id"]),
             (self.other_workspace.id, second["id"])},
        )
        self.assertEqual(
            self.store.get_mission_job(self.workspace.id, first["id"])["status"],
            "completed",
        )
        self.assertEqual(
            self.store.get_mission_job(self.other_workspace.id, second["id"])["status"],
            "completed",
        )

    def test_executor_is_required_and_result_must_be_json_serializable(self):
        with self.assertRaisesRegex(ValueError, "executor callable"):
            MissionWorkerSupervisor(self.store, None)
        job = self.store.create_mission_job(self.workspace.id, "Bad result", {})
        worker = MissionWorkerSupervisor(
            self.store, lambda _job, _context: {"not_json": object()},
            worker_id="worker-bad-result",
        )

        worker.run_once()

        saved = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(saved["status"], "retryable")
        self.assertIn("not JSON serializable", saved["last_error"])


if __name__ == "__main__":
    unittest.main()
