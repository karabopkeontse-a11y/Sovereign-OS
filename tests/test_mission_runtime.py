"""Durable mission queue persistence and lease recovery tests."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from sovereign_core.store import TaskStore


class MissionRuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "missions.sqlite3"
        self.store = TaskStore(self.db_path)
        self.workspace = self.store.create_workspace("Mission Workspace", "Queue tests")
        self.other_workspace = self.store.create_workspace("Other Workspace", "Isolation")

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_v6_preserves_existing_plugin_and_collaboration_tables(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            version = db.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(version, 6)
        self.assertTrue({
            "plugins", "workspace_metadata", "workspace_plugins",
            "agent_discussions", "agent_collaboration_runs",
            "agent_collaboration_checkpoints", "mission_jobs",
            "mission_job_events",
        }.issubset(tables))

    def test_create_is_idempotent_and_workspace_scoped(self):
        first = self.store.create_mission_job(
            self.workspace.id, "Research", {"topic": "Botswana"},
            idempotency_key="research-001",
        )
        replay = self.store.create_mission_job(
            self.workspace.id, "Research", {"topic": "Botswana"},
            idempotency_key="research-001",
        )
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["status"], "queued")
        with self.assertRaisesRegex(ValueError, "different input"):
            self.store.create_mission_job(
                self.workspace.id, "Different task", {"topic": "Botswana"},
                idempotency_key="research-001",
            )
        self.assertIsNone(
            self.store.get_mission_job(self.other_workspace.id, first["id"])
        )
        self.assertEqual(len(self.store.list_mission_jobs(self.workspace.id)), 1)

    def test_future_mission_is_not_claimed_early(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Scheduled research", {},
            scheduled_at="9999-01-01T00:00:00+00:00",
        )
        self.assertEqual(job["status"], "scheduled")
        self.assertIsNone(
            self.store.claim_mission_job(self.workspace.id, "worker-a")
        )
        self.assertEqual(
            self.store.get_mission_job(self.workspace.id, job["id"])["status"],
            "scheduled",
        )

    def test_only_one_concurrent_worker_claims_a_mission(self):
        job = self.store.create_mission_job(self.workspace.id, "One claim", {})
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(
                lambda n: self.store.claim_mission_job(
                    self.workspace.id, f"worker-{n}"
                ),
                range(8),
            ))
        winners = [claim for claim in claims if claim is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["id"], job["id"])
        self.assertEqual(winners[0]["attempts"], 1)

    def test_expired_worker_lease_can_be_reclaimed_after_restart(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Recover me", {}, max_attempts=3
        )
        first = self.store.claim_mission_job(self.workspace.id, "worker-old")
        self.assertEqual(first["id"], job["id"])
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE mission_jobs SET lease_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", job["id"]),
            )
            db.commit()
        restarted_store = TaskStore(self.db_path)
        recovered = restarted_store.claim_mission_job(
            self.workspace.id, "worker-new"
        )
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["worker_id"], "worker-new")
        self.assertEqual(recovered["attempts"], 2)
        event_types = [
            item["event_type"] for item in
            restarted_store.list_mission_job_events(self.workspace.id, job["id"])
        ]
        self.assertIn("lease_reclaimed", event_types)

    def test_expired_lease_at_attempt_limit_becomes_failed(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Exhausted", {}, max_attempts=1
        )
        claimed = self.store.claim_mission_job(self.workspace.id, "worker-a")
        self.assertEqual(claimed["id"], job["id"])
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE mission_jobs SET lease_expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", job["id"]),
            )
            db.commit()
        self.assertIsNone(
            self.store.claim_mission_job(self.workspace.id, "worker-b")
        )
        failed = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("maximum attempts", failed["last_error"])

    def test_heartbeat_requires_current_lease_owner(self):
        job = self.store.create_mission_job(self.workspace.id, "Heartbeat", {})
        self.store.claim_mission_job(self.workspace.id, "worker-a")
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.store.heartbeat_mission_job(
                self.workspace.id, job["id"], "worker-b"
            )
        updated = self.store.heartbeat_mission_job(
            self.workspace.id, job["id"], "worker-a", lease_seconds=120
        )
        self.assertTrue(updated["heartbeat_at"])
        self.assertTrue(updated["lease_expires_at"])

    def test_pause_resume_and_completion_are_guarded_and_audited(self):
        job = self.store.create_mission_job(self.workspace.id, "Lifecycle", {})
        self.store.claim_mission_job(self.workspace.id, "worker-a")
        with self.assertRaisesRegex(ValueError, "lease owner"):
            self.store.set_mission_job_status(
                self.workspace.id, job["id"], "completed",
                worker_id="worker-b",
            )
        paused = self.store.set_mission_job_status(
            self.workspace.id, job["id"], "paused",
            worker_id="worker-a", detail="Waiting for user",
        )
        self.assertEqual(paused["status"], "paused")
        self.assertIsNone(
            self.store.claim_mission_job(self.workspace.id, "worker-b")
        )
        self.store.set_mission_job_status(
            self.workspace.id, job["id"], "queued"
        )
        claimed = self.store.claim_mission_job(self.workspace.id, "worker-b")
        self.assertEqual(claimed["id"], job["id"])
        completed = self.store.set_mission_job_status(
            self.workspace.id, job["id"], "completed",
            worker_id="worker-b", result={"artifact": "report.md"},
        )
        self.assertEqual(completed["result"], {"artifact": "report.md"})
        self.assertIsNotNone(completed["completed_at"])
        self.assertEqual(
            [event["event_type"] for event in
             self.store.list_mission_job_events(self.workspace.id, job["id"])],
            ["created", "claimed", "paused", "queued", "claimed", "completed"],
        )
        with self.assertRaisesRegex(ValueError, "Invalid mission transition"):
            self.store.set_mission_job_status(
                self.workspace.id, job["id"], "queued"
            )


if __name__ == "__main__":
    unittest.main()
