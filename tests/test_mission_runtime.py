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

    def test_schema_v7_preserves_plugin_collaboration_and_mission_tables(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            version = db.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(version, 7)
        self.assertTrue({
            "plugins", "workspace_metadata", "workspace_plugins",
            "agent_discussions", "agent_collaboration_runs",
            "agent_collaboration_checkpoints", "mission_jobs",
            "mission_job_events", "mission_schedules",
            "mission_schedule_events",
        }.issubset(tables))

    def test_schedule_creation_is_idempotent_and_workspace_scoped(self):
        first = self.store.create_mission_schedule(
            self.workspace.id, "Morning research", {"topic": "Botswana"},
            "2026-11-24T08:00:00+02:00", "Africa/Gaborone",
            idempotency_key="morning-research",
        )
        replay = self.store.create_mission_schedule(
            self.workspace.id, "Morning research", {"topic": "Botswana"},
            "2026-11-24T08:00:00+02:00", "Africa/Gaborone",
            idempotency_key="morning-research",
        )
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["status"], "scheduled")
        self.assertIsNone(
            self.store.get_mission_schedule(self.other_workspace.id, first["id"])
        )
        with self.assertRaisesRegex(ValueError, "different input"):
            self.store.create_mission_schedule(
                self.workspace.id, "Changed mission", {"topic": "Botswana"},
                "2026-11-24T08:00:00+02:00", "Africa/Gaborone",
                idempotency_key="morning-research",
            )

    def test_schedule_requires_valid_explicit_timezone_and_offset(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.store.create_mission_schedule(
                self.workspace.id, "Naive", {}, "2026-11-24T08:00:00",
                "Africa/Gaborone",
            )
        with self.assertRaisesRegex(ValueError, "timezone_name"):
            self.store.create_mission_schedule(
                self.workspace.id, "Unknown zone", {},
                "2026-11-24T08:00:00+02:00", "Mars/Olympus",
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.store.create_mission_schedule(
                self.workspace.id, "Wrong offset", {},
                "2026-11-24T08:00:00+00:00", "Africa/Gaborone",
            )

    def test_due_schedule_promotes_once_into_existing_mission_queue(self):
        schedule = self.store.create_mission_schedule(
            self.workspace.id, "Due report", {"format": "markdown"},
            "2026-11-23T08:00:00+02:00", "Africa/Gaborone",
            priority=8, max_attempts=4, idempotency_key="due-report",
        )
        promoted = self.store.promote_due_mission_schedules(
            self.workspace.id, now="2026-11-23T17:00:00+02:00",
        )
        self.assertEqual(len(promoted), 1)
        job = promoted[0]
        self.assertEqual(job["title"], "Due report")
        self.assertEqual(job["payload"], {"format": "markdown"})
        self.assertEqual(job["priority"], 8)
        self.assertEqual(job["max_attempts"], 4)
        self.assertEqual(job["idempotency_key"], "schedule:" + schedule["id"])
        saved = self.store.get_mission_schedule(self.workspace.id, schedule["id"])
        self.assertEqual(saved["status"], "promoted")
        self.assertEqual(saved["mission_job_id"], job["id"])
        self.assertEqual(
            [event["event_type"] for event in
             self.store.list_mission_schedule_events(self.workspace.id, schedule["id"])],
            ["created", "promoted"],
        )
        self.assertEqual(
            [event["event_type"] for event in
             self.store.list_mission_job_events(self.workspace.id, job["id"])],
            ["created"],
        )
        self.assertEqual(
            self.store.promote_due_mission_schedules(
                self.workspace.id, now="2026-11-23T18:00:00+02:00",
            ),
            [],
        )
        self.assertEqual(len(self.store.list_mission_jobs(self.workspace.id)), 1)

    def test_future_and_cancelled_schedules_are_not_promoted(self):
        future = self.store.create_mission_schedule(
            self.workspace.id, "Future", {},
            "2026-11-24T08:00:00+02:00", "Africa/Gaborone",
        )
        cancelled = self.store.create_mission_schedule(
            self.workspace.id, "Cancelled", {},
            "2026-11-23T08:00:00+02:00", "Africa/Gaborone",
        )
        self.store.cancel_mission_schedule(
            self.workspace.id, cancelled["id"], "No longer needed",
        )
        self.assertEqual(
            self.store.promote_due_mission_schedules(
                self.workspace.id, now="2026-11-23T17:00:00+02:00",
            ),
            [],
        )
        self.assertEqual(
            self.store.get_mission_schedule(self.workspace.id, future["id"])["status"],
            "scheduled",
        )

    def test_concurrent_schedule_promotion_creates_one_queue_job(self):
        schedule = self.store.create_mission_schedule(
            self.workspace.id, "Concurrent", {},
            "2026-11-23T08:00:00+02:00", "Africa/Gaborone",
        )
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(
                lambda _: self.store.promote_due_mission_schedules(
                    self.workspace.id, now="2026-11-23T17:00:00+02:00",
                ),
                range(6),
            ))
        jobs = [job for batch in results for job in batch]
        self.assertEqual(len(jobs), 1)
        saved = self.store.get_mission_schedule(self.workspace.id, schedule["id"])
        self.assertEqual(saved["status"], "promoted")
        self.assertEqual(len(self.store.list_mission_jobs(self.workspace.id)), 1)

    def test_schedule_cannot_be_cancelled_after_promotion(self):
        schedule = self.store.create_mission_schedule(
            self.workspace.id, "Already due", {},
            "2026-11-23T08:00:00+02:00", "Africa/Gaborone",
        )
        self.store.promote_due_mission_schedules(
            self.workspace.id, now="2026-11-23T17:00:00+02:00",
        )
        with self.assertRaisesRegex(ValueError, "Only pending"):
            self.store.cancel_mission_schedule(self.workspace.id, schedule["id"])

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

    def test_priority_order_is_respected_for_eligible_missions(self):
        low = self.store.create_mission_job(
            self.workspace.id, "Low priority", {}, priority=1
        )
        high = self.store.create_mission_job(
            self.workspace.id, "High priority", {}, priority=10
        )
        claimed = self.store.claim_mission_job(
            self.workspace.id, "worker-priority"
        )
        self.assertEqual(claimed["id"], high["id"])
        self.assertNotEqual(claimed["id"], low["id"])

    def test_invalid_schedule_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "valid ISO timestamp"):
            self.store.create_mission_job(
                self.workspace.id, "Bad schedule", {},
                scheduled_at="tomorrow morning",
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

    def test_schema_v8_adds_recurring_schedules_without_losing_existing_tables(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(version, 8)
        self.assertTrue({
            "plugins", "workspace_metadata", "workspace_plugins",
            "agent_collaboration_runs", "mission_jobs", "mission_schedules",
            "mission_recurring_schedules", "mission_recurring_occurrences",
            "mission_recurring_schedule_events",
        }.issubset(tables))

    def test_daily_recurring_schedule_promotes_due_occurrences_idempotently(self):
        schedule = self.store.create_recurring_mission_schedule(
            self.workspace.id, "Daily briefing", {"kind": "briefing"},
            "daily", "08:30", "Africa/Gaborone", "2026-11-23",
            idempotency_key="daily-briefing",
        )
        replay = self.store.create_recurring_mission_schedule(
            self.workspace.id, "Daily briefing", {"kind": "briefing"},
            "daily", "08:30", "Africa/Gaborone", "2026-11-23",
            idempotency_key="daily-briefing",
        )
        self.assertEqual(schedule["id"], replay["id"])
        promoted = self.store.promote_due_recurring_mission_schedules(
            self.workspace.id, now="2026-11-25T09:00:00+02:00",
        )
        self.assertEqual(len(promoted), 3)
        self.assertEqual(len(self.store.list_mission_jobs(self.workspace.id)), 3)
        again = self.store.promote_due_recurring_mission_schedules(
            self.workspace.id, now="2026-11-25T10:00:00+02:00",
        )
        self.assertEqual(again, [])
        occurrences = self.store.list_recurring_mission_occurrences(
            self.workspace.id, schedule["id"],
        )
        self.assertEqual([item["status"] for item in occurrences], ["promoted"] * 3)
        self.assertEqual(
            len({item["mission_job_id"] for item in occurrences}), 3,
        )

    def test_weekly_schedule_only_runs_selected_weekdays(self):
        schedule = self.store.create_recurring_mission_schedule(
            self.workspace.id, "Weekly review", {}, "weekly", "09:00",
            "Africa/Gaborone", "2026-11-23", weekdays=[0, 2],
        )
        promoted = self.store.promote_due_recurring_mission_schedules(
            self.workspace.id, now="2026-11-30T12:00:00+02:00",
        )
        self.assertEqual(len(promoted), 3)
        dates = [
            item["local_date"] for item in
            self.store.list_recurring_mission_occurrences(self.workspace.id, schedule["id"])
        ]
        self.assertEqual(dates, ["2026-11-23", "2026-11-25", "2026-11-30"])

    def test_dst_nonexistent_time_can_be_skipped_or_shifted_forward(self):
        skipped = self.store.create_recurring_mission_schedule(
            self.workspace.id, "Skip DST gap", {}, "daily", "02:30",
            "America/New_York", "2027-03-14",
            dst_nonexistent_policy="skip", idempotency_key="skip-gap",
        )
        shifted = self.store.create_recurring_mission_schedule(
            self.workspace.id, "Shift DST gap", {}, "daily", "02:30",
            "America/New_York", "2027-03-14",
            dst_nonexistent_policy="shift_forward", idempotency_key="shift-gap",
        )
        self.assertEqual(skipped["next_local_date"], "2027-03-15")
        self.assertEqual(shifted["next_local_date"], "2027-03-14")
        self.assertEqual(
            shifted["next_run_utc"], "2027-03-14T07:00:00+00:00",
        )

    def test_dst_ambiguous_time_respects_first_and_second_occurrence(self):
        first = self.store.create_recurring_mission_schedule(
            self.workspace.id, "First fold", {}, "daily", "01:30",
            "America/New_York", "2027-11-07",
            dst_ambiguous_policy="first", idempotency_key="first-fold",
        )
        second = self.store.create_recurring_mission_schedule(
            self.workspace.id, "Second fold", {}, "daily", "01:30",
            "America/New_York", "2027-11-07",
            dst_ambiguous_policy="second", idempotency_key="second-fold",
        )
        self.assertEqual(
            first["next_run_utc"], "2027-11-07T05:30:00+00:00",
        )
        self.assertEqual(
            second["next_run_utc"], "2027-11-07T06:30:00+00:00",
        )

    def test_recurring_schedule_restart_recovery_and_concurrent_promotion(self):
        schedule = self.store.create_recurring_mission_schedule(
            self.workspace.id, "Concurrent daily", {}, "daily", "08:00",
            "Africa/Gaborone", "2026-11-23",
        )
        restarted = TaskStore(self.db_path)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(
                lambda _: restarted.promote_due_recurring_mission_schedules(
                    self.workspace.id, now="2026-11-23T09:00:00+02:00",
                ),
                range(6),
            ))
        jobs = [job for batch in results for job in batch]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(restarted.list_mission_jobs(self.workspace.id)), 1)
        self.assertEqual(
            restarted.get_recurring_mission_schedule(self.workspace.id, schedule["id"])["next_local_date"],
            "2026-11-24",
        )

    def test_disabled_and_cancelled_recurring_schedules_do_not_promote(self):
        disabled = self.store.create_recurring_mission_schedule(
            self.workspace.id, "Disabled", {}, "daily", "08:00",
            "Africa/Gaborone", "2026-11-23",
        )
        cancelled = self.store.create_recurring_mission_schedule(
            self.workspace.id, "Cancelled", {}, "daily", "08:00",
            "Africa/Gaborone", "2026-11-23",
        )
        self.store.set_recurring_mission_schedule_status(
            self.workspace.id, disabled["id"], "disabled",
        )
        self.store.set_recurring_mission_schedule_status(
            self.workspace.id, cancelled["id"], "cancelled",
        )
        self.assertEqual(
            self.store.promote_due_recurring_mission_schedules(
                self.workspace.id, now="2026-11-24T09:00:00+02:00",
            ), [],
        )
        self.assertEqual(self.store.list_mission_jobs(self.workspace.id), [])

