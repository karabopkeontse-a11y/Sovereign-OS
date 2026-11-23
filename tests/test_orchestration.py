import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from sovereign_core.orchestration import run_collaboration
from sovereign_core.store import TaskStore


class FakeStore:
    def __init__(self):
        self.identities = []
        self.discussions = []
        self.messages = []

    def list_agent_identities(self, workspace_id):
        return [x for x in self.identities if x["workspace_id"] == workspace_id]

    def create_agent_identity(self, **kwargs):
        item = {"id": f"agent-{len(self.identities)+1}", **kwargs}
        self.identities.append(item)
        return item

    def create_agent_discussion(self, **kwargs):
        item = {"id": f"discussion-{len(self.discussions)+1}", **kwargs}
        self.discussions.append(item)
        return item

    def create_agent_assignment(self, workspace_id, discussion_id, agent_id, task):
        if not hasattr(self, "assignments"):
            self.assignments = []
        item = {
            "id": f"assignment-{len(self.assignments) + 1}",
            "workspace_id": workspace_id,
            "discussion_id": discussion_id,
            "agent_id": agent_id,
            "task": task,
            "status": "assigned",
            "result": "",
        }
        self.assignments.append(item)
        return item

    def update_agent_assignment(self, workspace_id, assignment_id, status, result=""):
        item = next(
            x for x in self.assignments
            if x["id"] == assignment_id and x["workspace_id"] == workspace_id
        )
        item["status"] = status
        item["result"] = result
        return item

    def set_agent_discussion_status(self, workspace_id, discussion_id, status):
        item = next(
            x for x in self.discussions
            if x["id"] == discussion_id and x["workspace_id"] == workspace_id
        )
        item["status"] = status
        return item

    def list_agent_assignments(self, workspace_id, discussion_id):
        return [
            x for x in getattr(self, "assignments", [])
            if x["workspace_id"] == workspace_id
            and x["discussion_id"] == discussion_id
        ]

    def append_agent_message(self, workspace_id, discussion_id,
                             speaker_type, content, agent_id=None):
        self.messages.append({
            "workspace_id": workspace_id,
            "discussion_id": discussion_id,
            "speaker_type": speaker_type,
            "content": content,
            "agent_id": agent_id,
        })


    def list_agent_messages(self, workspace_id, discussion_id):
        return [
            message for message in self.messages
            if message["workspace_id"] == workspace_id
            and message["discussion_id"] == discussion_id
        ]


class FakeRouter:
    def __init__(self):
        self.candidates = [
            SimpleNamespace(info=SimpleNamespace(
                provider="groq", model="qwen-coder", runtime="cloud"
            )),
            SimpleNamespace(info=SimpleNamespace(
                provider="openrouter", model="deepseek-reasoner", runtime="cloud"
            )),
        ]
        self.calls = 0
        self.invocations = []
        self.fail_at = None

    def invoke(self, messages, start_index=None, **kwargs):
        index = start_index or 0
        info = self.candidates[index].info
        self.calls += 1
        if self.fail_at == self.calls:
            raise RuntimeError('Simulated provider interruption')
        prompt = "\n".join(
            str(getattr(message, "content", message))
            for message in messages
        )
        self.invocations.append({"index": index, "prompt": prompt})
        response = SimpleNamespace(
            content=f"Test response {self.calls}. Reviewed the available evidence."
        )
        return response, info, index


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.router = FakeRouter()
        self.workspace = SimpleNamespace(
            id="workspace-a",
            name="Nikolai Creative Studio",
            purpose="Creative work",
        )

    def test_agents_propose_review_and_leader_reports(self):
        result = run_collaboration(
            self.store, self.router, self.workspace,
            "Develop a practical project plan."
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["workspace_id"], "workspace-a")
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(len(result["peer_reviews"]), 2)
        self.assertTrue(result["report"])
        self.assertEqual(result["leader"]["name"], "Sovereign Lead")
        self.assertEqual(len(result["agent_responses"]), 2)
        self.assertEqual(len(self.store.messages), 8)
        self.assertEqual(
            [m["speaker_type"] for m in self.store.messages],
            ["user", "agent", "agent", "agent",
             "agent", "agent", "agent", "leader"],
        )

        self.assertTrue(all(
            m["agent_id"] for m in self.store.messages
            if m["speaker_type"] == "agent"
        ))
        self.assertTrue(all(
            m["discussion_id"] == result["discussion_id"]
            for m in self.store.messages
        ))

        response_prompts = [
            call["prompt"] for call in self.router.invocations
            if "Persisted discussion so far:" in call["prompt"]
        ]
        self.assertEqual(len(response_prompts), 2)
        self.assertTrue(all(
            "provided this peer review:" in prompt
            and "Respond specifically to that colleague." in prompt
            for prompt in response_prompts
        ))
        self.assertTrue(all(
            "Test response 3" in prompt or "Test response 4" in prompt
            for prompt in response_prompts
        ))
        self.assertTrue(all(
            m["workspace_id"] == "workspace-a"
            for m in self.store.messages
        ))

    def test_selected_specialists_and_assignment_progress_are_returned(self):
        result = run_collaboration(
            self.store, self.router, self.workspace,
            "Develop a practical project plan.",
            specialist_ids=["specialist-0", "specialist-1"],
        )
        self.assertEqual(len(result["proposals"]), 2)
        self.assertEqual(len(result["peer_reviews"]), 2)
        self.assertEqual(len(result["assignments"]), 3)
        self.assertTrue(all(
            item["status"] == "completed"
            for item in result["assignments"]
        ))

    def test_unknown_specialist_is_rejected_before_creating_records(self):
        with self.assertRaises(ValueError):
            run_collaboration(
                self.store, self.router, self.workspace,
                "Develop a practical project plan.",
                specialist_ids=["specialist-0", "specialist-99"],
            )
        self.assertEqual(self.store.identities, [])
        self.assertEqual(self.store.discussions, [])

    def test_agent_identities_are_reused(self):
        run_collaboration(
            self.store, self.router, self.workspace, "First task"
        )
        first_count = len(self.store.identities)

        run_collaboration(
            self.store, self.router, self.workspace, "Second task"
        )

        self.assertEqual(len(self.store.identities), first_count)
        self.assertEqual(len(self.store.discussions), 2)

    def test_empty_task_is_rejected(self):
        with self.assertRaises(ValueError):
            run_collaboration(
                self.store, self.router, self.workspace, "   "
            )

    def test_missing_models_are_rejected(self):
        self.router.candidates = []
        with self.assertRaises(RuntimeError):
            run_collaboration(
                self.store, self.router, self.workspace, "Test task"
            )



class DurableOrchestrationRecoveryTests(unittest.TestCase):
    def test_retry_reuses_completed_checkpoints_and_does_not_duplicate_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "durable.sqlite3")
            workspace = store.create_workspace(
                "Durable Test", "Test resumable collaboration"
            )
            leader = store.create_agent_identity(
                workspace.id, "leader", "Sovereign Lead"
            )
            discussion = store.create_agent_discussion(
                workspace.id, "Resume this mission", leader["id"]
            )
            run = store.create_agent_collaboration_run(
                workspace.id, discussion["id"], "Prepare a safe launch plan",
                specialist_ids=["specialist-0", "specialist-1"],
                max_agents=2, request_key="durable-retry-test",
            )
            router = FakeRouter()
            router.fail_at = 4

            with self.assertRaisesRegex(RuntimeError, "Simulated provider interruption"):
                run_collaboration(
                    store, router, workspace, run["task"],
                    specialist_ids=["specialist-0", "specialist-1"],
                    run_id=run["id"], discussion_id=discussion["id"],
                )

            saved = store.list_agent_collaboration_checkpoints(
                workspace.id, run["id"]
            )
            self.assertEqual(
                sum(item["status"] == "completed" for item in saved), 3
            )
            self.assertEqual(
                len(store.list_agent_messages(workspace.id, discussion["id"])), 4
            )
            self.assertEqual(
                store.get_agent_collaboration_run(workspace.id, run["id"])["status"],
                "retryable",
            )

            router.fail_at = None
            result = run_collaboration(
                store, router, workspace, run["task"],
                specialist_ids=["specialist-0", "specialist-1"],
                run_id=run["id"], discussion_id=discussion["id"],
            )

            checkpoints = store.list_agent_collaboration_checkpoints(
                workspace.id, run["id"]
            )
            messages = store.list_agent_messages(workspace.id, discussion["id"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(checkpoints), 7)
            self.assertTrue(all(item["status"] == "completed" for item in checkpoints))
            self.assertEqual(len(messages), 8)
            self.assertEqual(sum(m["speaker_type"] == "user" for m in messages), 1)
            self.assertEqual(router.calls, 8)


if __name__ == "__main__":
    unittest.main()
