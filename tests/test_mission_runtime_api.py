"""API integration tests for durable registered-agent mission enqueue and runtime."""
import json
import tempfile
import time
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from sovereign_core.api import create_app
except ImportError:
    TestClient = None
    create_app = None

from sovereign_core.store import TaskStore


@unittest.skipIf(TestClient is None, "Install requirements-api.txt to run REST API tests")
class MissionRuntimeAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "mission-api.sqlite3"
        self.app = create_app(self.db_path, token="test-secret")
        self.store = TaskStore(self.db_path)
        self.workspace = next(
            item for item in self.store.list_workspaces()
            if item.name == "SovereignOS"
        )
        self.headers = {"Authorization": "Bearer test-secret"}

    def tearDown(self):
        self.temp.cleanup()

    def create_step(self, *, permission="read_only", approved=False):
        problem = self.store.create_problem(
            self.workspace.id, "Queue a registered step", "Exercise durable mission enqueue."
        )
        self.store.set_problem_status(problem.id, "diagnosing")
        self.store.set_problem_status(problem.id, "planned")
        step = self.store.add_plan_step(
            problem.id, 1, "List workspace files", permission
        )
        self.store.add_step_spec(
            step.id, "workspace.list_files", json.dumps({}),
            "The scoped workspace file list is returned.",
        )
        if approved:
            self.store.update_step_status(step.id, "approved")
            self.store.record_approval_event(
                self.workspace.id, problem.id, step.id, "approved",
                "Approved by the workspace owner.",
            )
        return problem, step

    def test_enqueue_is_durable_idempotent_and_workspace_scoped(self):
        problem, step = self.create_step()
        with TestClient(self.app) as client:
            path = (
                f"/api/v1/workspaces/{self.workspace.id}/agent/tasks/"
                f"{problem.id}/steps/{step.id}/enqueue"
            )
            first = client.post(path, headers=self.headers)
            self.assertEqual(first.status_code, 202)
            mission = first.json()["mission"]
            self.assertEqual(mission["status"], "queued")
            self.assertEqual(mission["payload"]["kind"], "registered_agent_step")
            self.assertEqual(mission["payload"]["problem_id"], problem.id)
            self.assertEqual(mission["payload"]["step_id"], step.id)

            # Simulate the worker claiming the step before the client retries.
            self.store.update_step_status(step.id, "running")
            replay = client.post(path, headers=self.headers)
            self.assertEqual(replay.status_code, 202)
            self.assertEqual(replay.json()["mission"]["id"], mission["id"])

            listed = client.get(
                f"/api/v1/workspaces/{self.workspace.id}/missions",
                headers=self.headers,
            )
            self.assertEqual(listed.status_code, 200)
            self.assertEqual([item["id"] for item in listed.json()["missions"]], [mission["id"]])

            detail = client.get(
                f"/api/v1/workspaces/{self.workspace.id}/missions/{mission['id']}",
                headers=self.headers,
            )
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(
                [event["event_type"] for event in detail.json()["events"]],
                ["created"],
            )

            other = self.store.create_workspace("Private Other", "Isolation")
            foreign = client.get(
                f"/api/v1/workspaces/{other.id}/missions/{mission['id']}",
                headers=self.headers,
            )
            self.assertEqual(foreign.status_code, 404)

    def test_unapproved_effect_cannot_be_enqueued(self):
        problem, step = self.create_step(permission="external_effect")
        with TestClient(self.app) as client:
            path = (
                f"/api/v1/workspaces/{self.workspace.id}/agent/tasks/"
                f"{problem.id}/steps/{step.id}/enqueue"
            )
            rejected = client.post(path, headers=self.headers)
            self.assertEqual(rejected.status_code, 403)
            self.assertEqual(
                self.store.list_mission_jobs(self.workspace.id),
                [],
            )

            self.store.update_step_status(step.id, "approved")
            self.store.record_approval_event(
                self.workspace.id, problem.id, step.id, "approved",
                "Owner approved this step.",
            )
            accepted = client.post(path, headers=self.headers)
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(accepted.json()["mission"]["status"], "queued")

    def test_later_step_cannot_be_queued_before_predecessor_verifies(self):
        problem, first = self.create_step()
        second = self.store.add_plan_step(
            problem.id, 2, "Second ordered step", "read_only"
        )
        self.store.add_step_spec(
            second.id, "workspace.list_files", json.dumps({}),
            "The second scoped file list is returned.",
        )
        with TestClient(self.app) as client:
            path = (
                f"/api/v1/workspaces/{self.workspace.id}/agent/tasks/"
                f"{problem.id}/steps/{second.id}/enqueue"
            )
            rejected = client.post(path, headers=self.headers)
            self.assertEqual(rejected.status_code, 409)

            first_path = (
                f"/api/v1/workspaces/{self.workspace.id}/agent/tasks/"
                f"{problem.id}/steps/{first.id}/enqueue"
            )
            accepted = client.post(first_path, headers=self.headers)
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(len(self.store.list_mission_jobs(self.workspace.id)), 1)

    def test_worker_is_disabled_by_default(self):
        with TestClient(self.app) as client:
            health = client.get(
                "/api/v1/mission-worker/health", headers=self.headers
            )
            self.assertEqual(health.status_code, 200)
            self.assertEqual(
                health.json(), {"enabled": False, "state": "disabled"}
            )

    def test_desktop_runtime_executes_only_registered_agent_missions(self):
        problem, step = self.create_step()
        app = create_app(
            self.db_path, token="test-secret",
            enable_mission_worker=True, mission_worker_poll_seconds=0.02,
        )
        with TestClient(app) as client:
            health = client.get(
                "/api/v1/mission-worker/health", headers=self.headers
            )
            self.assertTrue(health.json()["enabled"])
            self.assertIn(health.json()["state"], {"starting", "running", "healthy"})

            queued = client.post(
                f"/api/v1/workspaces/{self.workspace.id}/agent/tasks/"
                f"{problem.id}/steps/{step.id}/enqueue",
                headers=self.headers,
            )
            self.assertEqual(queued.status_code, 202)
            mission_id = queued.json()["mission"]["id"]

            deadline = time.monotonic() + 8
            saved = None
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/v1/workspaces/{self.workspace.id}/missions/{mission_id}",
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 200)
                saved = response.json()["mission"]
                if saved["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.03)

            self.assertIsNotNone(saved)
            self.assertEqual(saved["status"], "completed")
            self.assertTrue(saved["result"]["verified"])
            self.assertEqual(self.store.get_step(step.id).status, "verified")


if __name__ == "__main__":
    unittest.main()
