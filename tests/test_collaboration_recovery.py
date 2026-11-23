"""Durable agent collaboration recovery persistence tests."""
import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

from sovereign_core.store import TaskStore


class CollaborationRecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "recovery.sqlite3")
        self.workspace = self.store.create_workspace("Recovery Test", "Testing recovery")
        self.agent = self.store.create_agent_identity(
            self.workspace.id, "specialist", "Research Agent"
        )
        self.discussion = self.store.create_agent_discussion(
            self.workspace.id, "Recoverable mission", self.agent["id"]
        )
        self.run = self.store.create_agent_collaboration_run(
            self.workspace.id, self.discussion["id"],
            "Research a reliable launch plan", memory_context="Existing context",
            specialist_ids=["specialist-0"], max_agents=2,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_v5_is_applied_without_removing_existing_tables(self):
        # sqlite3.Connection.__exit__ commits/rolls back but does not close;
        # explicitly close it so Windows can remove the TemporaryDirectory.
        with closing(sqlite3.connect(self.store.db_path)) as db:
            version = db.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            run_columns = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(agent_collaboration_runs)"
                )
            }
        self.assertEqual(version, 5)
        self.assertIn("request_key", run_columns)
        self.assertTrue({
            "plugins", "workspace_metadata", "workspace_plugins",
            "agent_discussions", "agent_assignments",
            "agent_collaboration_runs", "agent_collaboration_checkpoints",
        }.issubset(tables))

    def test_discussion_and_run_are_created_atomically_for_one_key(self):
        leader = self.store.create_agent_identity(
            self.workspace.id, "leader", "Sovereign Lead"
        )
        first = self.store.create_agent_collaboration_run_with_discussion(
            self.workspace.id, leader["id"], "Atomic mission",
            specialist_ids=["specialist-0", "specialist-1"],
            max_agents=2, request_key="atomic-mission-001",
        )
        second = self.store.create_agent_collaboration_run_with_discussion(
            self.workspace.id, leader["id"], "Atomic mission",
            specialist_ids=["specialist-0", "specialist-1"],
            max_agents=2, request_key="atomic-mission-001",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["discussion_id"], second["discussion_id"])
        matching = [
            item for item in self.store.list_agent_discussions(self.workspace.id)
            if item["title"] == "Atomic mission"
        ]
        self.assertEqual(len(matching), 1)

    def test_idempotency_key_returns_the_original_run(self):
        first = self.store.create_agent_collaboration_run(
            self.workspace.id, self.discussion["id"],
            "Prepare a reliable launch plan", specialist_ids=["specialist-0"],
            max_agents=2, request_key="mission-request-001",
        )
        second = self.store.create_agent_collaboration_run(
            self.workspace.id, self.discussion["id"],
            "Prepare a reliable launch plan", specialist_ids=["specialist-0"],
            max_agents=2, request_key="mission-request-001",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["request_key"], "mission-request-001")

    def test_idempotency_key_cannot_be_reused_for_another_task(self):
        self.store.create_agent_collaboration_run(
            self.workspace.id, self.discussion["id"],
            "Prepare a reliable launch plan", request_key="mission-request-002",
        )
        with self.assertRaisesRegex(ValueError, "different mission"):
            self.store.create_agent_collaboration_run(
                self.workspace.id, self.discussion["id"],
                "Publish an unrelated announcement",
                request_key="mission-request-002",
            )

    def test_only_one_worker_can_claim_a_queued_run(self):
        self.assertTrue(self.store.claim_agent_collaboration_run(
            self.workspace.id, self.run["id"]
        ))
        self.assertFalse(self.store.claim_agent_collaboration_run(
            self.workspace.id, self.run["id"]
        ))
        self.assertEqual(
            self.store.get_agent_collaboration_run(
                self.workspace.id, self.run["id"]
            )["status"],
            "running",
        )

    def test_run_and_checkpoints_are_persisted_and_workspace_scoped(self):
        checkpoint = self.store.create_agent_collaboration_checkpoint(
            self.workspace.id, self.run["id"], self.discussion["id"],
            "proposals", self.agent["id"],
        )
        self.assertEqual(checkpoint["status"], "pending")
        self.assertEqual(
            self.store.get_agent_collaboration_run(
                self.workspace.id, self.run["id"]
            )["task"],
            "Research a reliable launch plan",
        )
        self.assertEqual(
            len(self.store.list_agent_collaboration_checkpoints(
                self.workspace.id, self.run["id"]
            )),
            1,
        )
        self.assertIsNone(self.store.get_agent_collaboration_run(
            "another-workspace", self.run["id"]
        ))
        with self.assertRaises(ValueError):
            self.store.list_agent_collaboration_checkpoints(
                "another-workspace", self.run["id"]
            )

    def test_checkpoint_completion_and_message_are_atomic_and_idempotent(self):
        checkpoint = self.store.create_agent_collaboration_checkpoint(
            self.workspace.id, self.run["id"], self.discussion["id"],
            "proposals", self.agent["id"],
        )
        first = self.store.complete_agent_collaboration_checkpoint(
            self.workspace.id, checkpoint["id"],
            "Evidence-based proposal", "agent", self.agent["id"],
            provider="Groq", model="free-model",
        )
        second = self.store.complete_agent_collaboration_checkpoint(
            self.workspace.id, checkpoint["id"],
            "Evidence-based proposal", "agent", self.agent["id"],
            provider="Groq", model="free-model",
        )
        messages = self.store.list_agent_messages(
            self.workspace.id, self.discussion["id"]
        )
        self.assertFalse(first["already_completed"])
        self.assertTrue(second["already_completed"])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "Evidence-based proposal")
        self.assertEqual(first["message"]["id"], second["message"]["id"])
        self.assertEqual(second["checkpoint"]["status"], "completed")

    def test_checkpoint_cannot_be_completed_by_another_agent(self):
        checkpoint = self.store.create_agent_collaboration_checkpoint(
            self.workspace.id, self.run["id"], self.discussion["id"],
            "proposals", self.agent["id"],
        )
        with self.assertRaises(ValueError):
            self.store.complete_agent_collaboration_checkpoint(
                self.workspace.id, checkpoint["id"], "Wrong owner",
                "agent", "foreign-agent",
            )

    def test_run_status_and_stage_progress_survive_reopening_store(self):
        self.store.update_agent_collaboration_run(
            self.workspace.id, self.run["id"],
            status="running", current_stage="proposals",
            increment_attempts=True,
        )
        reopened = TaskStore(self.store.db_path)
        recovered = reopened.get_agent_collaboration_run(
            self.workspace.id, self.run["id"]
        )
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(recovered["current_stage"], "proposals")
        self.assertEqual(recovered["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
