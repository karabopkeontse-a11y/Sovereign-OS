"""REST API authentication, workspace isolation and IoT safety tests."""
import tempfile
import unittest
from pathlib import Path
try:
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient
    from sovereign_core.api import create_app
except ImportError:
    TestClient = None
    create_app = None

class FakeRouter:
    configuration_warnings = []
    def health_snapshot(self):
        return [{"provider":"Test","model":"fake","runtime":"cloud","status":"ready",
                 "cooldown_reason":"","failures":0,"successes":0}]
    def select_model(self, provider, model):
        if provider != "Test" or model != "fake":
            raise ValueError("That model is not configured")
        return {"provider": provider, "model": model, "runtime": "cloud", "active": True}
    def invoke(self, messages, **kwargs):
        class Reply:
            content = "Test response"
        class Info:
            provider = "Test"
            model = "fake"
            runtime = "cloud"
        return Reply(), Info(), 0

class FakeQwenOnlyRouter(FakeRouter):
    candidates = [type("Candidate", (), {
        "info": type("Info", (), {"provider": "Groq", "model": "qwen/qwen3.8-27b"})()
    })()]
    def invoke(self, messages, **kwargs):
        raise AssertionError("Qwen must not be invoked by SovereignOS")


class FakeMixedRouter(FakeRouter):
    candidates = [
        type("Candidate", (), {
            "info": type("Info", (), {"provider": "Groq", "model": "qwen/qwen3.8-27b"})()
        })(),
        type("Candidate", (), {
            "info": type("Info", (), {"provider": "OpenRouter", "model": "nvidia/nemotron-3.5-super-vl:free"})()
        })(),
    ]
    def __init__(self):
        self.candidate_indices = None
    def invoke(self, messages, **kwargs):
        self.candidate_indices = kwargs.get("candidate_indices")
        return super().invoke(messages, **kwargs)


class FakePlanningRouter(FakeRouter):
    def invoke(self, messages, **kwargs):
        class Reply:
            content = ('{"title":"Prepare a safe launch plan",'
                       '"description":"Prepare the workspace launch materials and review before publication.",'
                       '"steps":[{"action":"Research current launch requirements"},'
                       '{"action":"Draft launch copy in the workspace"},'
                       '{"action":"Publish the launch announcement"}]}')
        class Info:
            provider = "Test"
            model = "planner"
            runtime = "local"
        return Reply(), Info(), 0


class FakeToolPlanningRouter(FakeRouter):
    def invoke(self, messages, **kwargs):
        class Reply:
            content = ('{"title":"Prepare a local workspace draft",'
                       '"description":"Create a draft inside this workspace agent folder.",'
                       '"steps":[{"action":"List current workspace files",'
                       '"tool_name":"workspace.list_files","arguments":{},'
                       '"verification_criterion":"The file list is returned from the scoped folder."},'
                       '{"action":"Create the approved Markdown draft",'
                       '"tool_name":"workspace.create_note",'
                       '"arguments":{"filename":"approved-draft.md","content":"# Local draft\\nCreated by the approved agent tool."},'
                       '"verification_criterion":"Read the new note back and confirm its content."}]}')
        class Info:
            provider = "Test"
            model = "planner"
            runtime = "local"
        return Reply(), Info(), 0


class FakeCloudOnlyRouter(FakePlanningRouter):
    calls = 0
    candidates = [type("Candidate", (), {
        "info": type("Info", (), {"provider": "Test Cloud", "model": "safe-cloud-model", "runtime": "cloud"})()
    })()]
    def invoke(self, messages, **kwargs):
        self.calls += 1
        return super().invoke(messages, **kwargs)


@unittest.skipIf(TestClient is None, "Install requirements-api.txt to run REST API tests")
class SovereignAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.temp.name)/"api.sqlite3",
                                             router=FakeRouter(), token="test-secret"))
        self.headers = {"Authorization":"Bearer test-secret"}
    def tearDown(self):
        self.temp.cleanup()
    def test_voice_transcription_requires_auth(self):
        result = self.client.post(
            "/api/v1/voice/transcribe",
            content=b"audio",
            headers={"Content-Type": "audio/webm"},
        )
        self.assertEqual(result.status_code, 401)

    def test_voice_transcription_rejects_unsupported_media_type(self):
        result = self.client.post(
            "/api/v1/voice/transcribe",
            content=b"audio",
            headers={**self.headers, "Content-Type": "text/plain"},
        )
        self.assertEqual(result.status_code, 415)

    def test_voice_transcription_rejects_empty_audio(self):
        result = self.client.post(
            "/api/v1/voice/transcribe",
            content=b"",
            headers={**self.headers, "Content-Type": "audio/webm"},
        )
        self.assertEqual(result.status_code, 422)

    def test_health_public_workspace_api_protected(self):
        self.assertEqual(self.client.get("/api/v1/health").status_code,200)
        self.assertEqual(self.client.get("/api/v1/workspaces").status_code,401)
    def test_invalid_token_rejected(self):
        self.assertEqual(self.client.get("/api/v1/workspaces",
                         headers={"Authorization":"Bearer bad"}).status_code,401)
    def test_default_workspaces_listed(self):
        data=self.client.get("/api/v1/workspaces",headers=self.headers).json()
        names={w["name"] for w in data["workspaces"]}
        self.assertTrue({"SovereignOS","SmartFlow BW","Nikolai Creative Studio","Yard Truths"}.issubset(names))
    def test_conversations_scoped_to_workspace(self):
        ws=self.client.get("/api/v1/workspaces",headers=self.headers).json()["workspaces"]
        a=next(w for w in ws if w["name"]=="Nikolai Creative Studio")
        b=next(w for w in ws if w["name"]=="Yard Truths")
        created=self.client.post("/api/v1/workspaces/"+a["id"]+"/chat",
                                 headers=self.headers,json={"message":"NCS private test"})
        self.assertEqual(created.status_code,200)
        self.assertEqual(len(self.client.get("/api/v1/workspaces/"+a["id"]+"/conversations",
                         headers=self.headers).json()["conversations"]),1)
        self.assertEqual(self.client.get("/api/v1/workspaces/"+b["id"]+"/conversations",
                         headers=self.headers).json()["conversations"],[])
    def test_cross_workspace_conversation_id_rejected(self):
        ws=self.client.get("/api/v1/workspaces",headers=self.headers).json()["workspaces"]
        a,b=ws[0],ws[1]
        created=self.client.post("/api/v1/workspaces/"+a["id"]+"/chat",
                                 headers=self.headers,json={"message":"private"})
        result=self.client.post("/api/v1/workspaces/"+b["id"]+"/chat",
                                headers=self.headers,json={"message":"cross scope",
                                "conversation_id":created.json()["conversation_id"]})
        self.assertEqual(result.status_code,404)
    def test_conversation_messages_are_workspace_scoped(self):
        ws=self.client.get("/api/v1/workspaces",headers=self.headers).json()["workspaces"]
        a,b=ws[0],ws[1]
        created=self.client.post("/api/v1/workspaces/"+a["id"]+"/chat",headers=self.headers,json={"message":"history check"})
        cid=created.json()["conversation_id"]
        own=self.client.get("/api/v1/workspaces/"+a["id"]+"/conversations/"+cid+"/messages",headers=self.headers)
        other=self.client.get("/api/v1/workspaces/"+b["id"]+"/conversations/"+cid+"/messages",headers=self.headers)
        self.assertEqual(own.status_code,200)
        self.assertEqual(len(own.json()["messages"]),2)
        self.assertEqual(other.status_code,404)
    def test_model_health_comes_from_router(self):
        data=self.client.get("/api/v1/models/health",headers=self.headers).json()
        self.assertEqual(data["providers"][0]["provider"],"Test")
    def test_manual_model_selection_requires_configured_candidate(self):
        ok=self.client.post("/api/v1/models/select",headers=self.headers,json={"provider":"Test","model":"fake"})
        self.assertEqual(ok.status_code,200)
        bad=self.client.post("/api/v1/models/select",headers=self.headers,json={"provider":"Other","model":"unknown"})
        self.assertEqual(bad.status_code,409)

    def test_chat_excludes_qwen_and_dynamic_aliases_from_fallback(self):
        router = FakeMixedRouter()
        client = TestClient(create_app(Path(self.temp.name)/"chat-allowlist.sqlite3",
                                       router=router, token="test-secret"))
        workspace = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"][0]
        result = client.post("/api/v1/workspaces/"+workspace["id"]+"/chat",
                             headers=self.headers, json={"message":"Hello Sovereign"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(router.candidate_indices, [1])

    def test_chat_fails_closed_when_only_qwen_is_configured(self):
        client = TestClient(create_app(Path(self.temp.name)/"chat-qwen-only.sqlite3",
                                       router=FakeQwenOnlyRouter(), token="test-secret"))
        workspace = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"][0]
        result = client.post("/api/v1/workspaces/"+workspace["id"]+"/chat",
                             headers=self.headers, json={"message":"Hello Sovereign"})
        self.assertEqual(result.status_code, 409)
        self.assertIn("Qwen", result.json()["detail"])

    def test_manual_model_selection_rejects_qwen(self):
        client = TestClient(create_app(Path(self.temp.name)/"select-qwen.sqlite3",
                                       router=FakeQwenOnlyRouter(), token="test-secret"))
        result = client.post("/api/v1/models/select", headers=self.headers,
                             json={"provider":"Groq","model":"qwen/qwen3.8-27b"})
        self.assertEqual(result.status_code, 409)
    def test_iot_data_is_marked_simulated(self):
        data=self.client.get("/api/v1/iot/devices",headers=self.headers).json()
        self.assertTrue(data["simulation"])
        self.assertTrue(all(x["status"]=="simulation" for x in data["devices"]))
    def test_iot_telemetry_endpoint_is_simulation_only(self):
        result=self.client.get("/api/v1/iot/devices/demo-water-01/telemetry",headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.json()["simulation"])
        self.assertEqual(result.json()["unit"],"%")
    def test_unknown_iot_device_telemetry_is_404(self):
        result=self.client.get("/api/v1/iot/devices/unknown/telemetry",headers=self.headers)
        self.assertEqual(result.status_code,404)
    def test_physical_device_commands_blocked(self):
        result=self.client.post("/api/v1/iot/commands",headers=self.headers,
                                json={"command":"open_valve"})
        self.assertEqual(result.status_code,403)


    def test_agent_plan_is_persisted_and_sensitive_steps_need_approval(self):
        client = TestClient(create_app(Path(self.temp.name)/"planner.sqlite3",
                                       router=FakePlanningRouter(), token="test-secret"))
        workspaces = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"]
        workspace = next(w for w in workspaces if w["name"] == "Nikolai Creative Studio")
        result = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/plan",
                             headers=self.headers,
                             json={"goal":"Prepare a safe launch plan for the new campaign."})
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        self.assertFalse(data["execution_started"])
        self.assertEqual(len(data["steps"]), 3)
        self.assertEqual(data["steps"][0]["permission_level"], "read_only")
        self.assertEqual(data["steps"][1]["status"], "awaiting_approval")
        self.assertEqual(data["steps"][2]["permission_level"], "high_impact")
        self.assertEqual(data["task"]["status"], "awaiting_approval")

    def test_approval_queue_is_workspace_scoped_and_audited(self):
        client = TestClient(create_app(Path(self.temp.name)/"approval.sqlite3",
                                       router=FakePlanningRouter(), token="test-secret"))
        workspaces = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"]
        workspace = next(w for w in workspaces if w["name"] == "Yard Truths")
        created = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/plan",
                              headers=self.headers,
                              json={"goal":"Prepare a safe launch plan for the new campaign."})
        self.assertEqual(created.status_code, 200, created.text)
        pending = client.get("/api/v1/approvals?workspace_id="+workspace["id"],
                             headers=self.headers).json()["approvals"]
        self.assertEqual(len(pending), 2)
        first = pending[0]
        decision = client.post(
            "/api/v1/workspaces/"+workspace["id"]+"/agent/tasks/"+first["problem_id"]+
            "/steps/"+first["step_id"]+"/decision",
            headers=self.headers, json={"approved":True,"note":"Reviewed by owner."})
        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertFalse(decision.json()["execution_started"])
        tasks = client.get("/api/v1/workspaces/"+workspace["id"]+"/agent/tasks",
                           headers=self.headers).json()["tasks"]
        events = tasks[0]["approval_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["decision"], "approved")
        remaining = client.get("/api/v1/approvals?workspace_id="+workspace["id"],
                               headers=self.headers).json()["approvals"]
        self.assertEqual(len(remaining), 1)

    def test_agent_task_cannot_be_read_from_another_workspace(self):
        client = TestClient(create_app(Path(self.temp.name)/"scope.sqlite3",
                                       router=FakePlanningRouter(), token="test-secret"))
        workspaces = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"]
        a = next(w for w in workspaces if w["name"] == "Nikolai Creative Studio")
        b = next(w for w in workspaces if w["name"] == "Yard Truths")
        created = client.post("/api/v1/workspaces/"+a["id"]+"/agent/plan",
                              headers=self.headers,
                              json={"goal":"Prepare a safe launch plan for the new campaign."})
        problem_id = created.json()["task"]["id"]
        other = client.get("/api/v1/workspaces/"+b["id"]+"/agent/tasks/"+problem_id,
                           headers=self.headers)
        self.assertEqual(other.status_code, 404)


    def test_agent_planner_fails_closed_when_only_qwen_is_configured(self):
        client = TestClient(create_app(Path(self.temp.name)/"qwen.sqlite3",
                                       router=FakeQwenOnlyRouter(), token="test-secret"))
        workspaces = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"]
        workspace = next(w for w in workspaces if w["name"] == "SovereignOS")
        result = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/plan",
                             headers=self.headers,
                             json={"goal":"Prepare a safe plan for a project milestone."})
        self.assertEqual(result.status_code, 409)
        self.assertIn("Qwen remains offline", result.json()["detail"])

    def test_voice_status_is_authenticated_and_reports_dependencies(self):
        denied = self.client.get("/api/v1/voice/status")
        self.assertEqual(denied.status_code, 401)
        allowed = self.client.get("/api/v1/voice/status", headers=self.headers)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("available", allowed.json())
        self.assertIn("ffmpeg_available", allowed.json())


    def test_agent_tools_run_only_in_workspace_and_writes_require_approval(self):
        client = TestClient(create_app(Path(self.temp.name)/"tools.sqlite3",
                                       router=FakeToolPlanningRouter(), token="test-secret"))
        workspaces = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"]
        workspace = next(w for w in workspaces if w["name"] == "SovereignOS")
        created = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/plan",
                              headers=self.headers,
                              json={"goal":"Create and verify a local Markdown draft in this workspace."})
        self.assertEqual(created.status_code, 200, created.text)
        task_id = created.json()["task"]["id"]
        first = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/tasks/"+task_id+"/execute-next",
                            headers=self.headers, json={})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["verified"])
        self.assertEqual(first.json()["tool_result"]["tool"], "workspace.list_files")
        blocked = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/tasks/"+task_id+"/execute-next",
                              headers=self.headers, json={})
        self.assertEqual(blocked.status_code, 409)
        pending = client.get("/api/v1/approvals?workspace_id="+workspace["id"],
                             headers=self.headers).json()["approvals"]
        self.assertEqual(len(pending), 1)
        unapproved_file = Path(self.temp.name) / "agent_workspace" / workspace["id"] / "approved-draft.md"
        self.assertFalse(unapproved_file.exists())
        self.assertEqual(pending[0]["tool_name"], "workspace.create_note")
        self.assertEqual(pending[0]["requested_details"]["filename"], "approved-draft.md")
        self.assertIn("Created by the approved agent tool",
                      pending[0]["requested_details"]["content"])
        approval = pending[0]
        decision = client.post(
            "/api/v1/workspaces/"+workspace["id"]+"/agent/tasks/"+task_id+
            "/steps/"+approval["step_id"]+"/decision",
            headers=self.headers, json={"approved":True,"note":"Approved local draft creation."})
        self.assertEqual(decision.status_code, 200, decision.text)
        second = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/tasks/"+task_id+"/execute-next",
                             headers=self.headers, json={})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["verified"])
        self.assertEqual(second.json()["task_status"], "completed")
        created_file = Path(self.temp.name) / "agent_workspace" / workspace["id"] / "approved-draft.md"
        self.assertTrue(created_file.is_file())
        self.assertIn("Created by the approved agent tool", created_file.read_text(encoding="utf-8"))


    def test_rejecting_a_step_blocks_task_and_cancels_other_pending_steps(self):
        client = TestClient(create_app(Path(self.temp.name)/"reject.sqlite3",
                                       router=FakePlanningRouter(), token="test-secret"))
        workspaces = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"]
        workspace = next(w for w in workspaces if w["name"] == "Nikolai Creative Studio")
        created = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/plan",
                              headers=self.headers,
                              json={"goal":"Prepare a safe launch plan for the new campaign."})
        task_id = created.json()["task"]["id"]
        pending = client.get("/api/v1/approvals?workspace_id="+workspace["id"],
                             headers=self.headers).json()["approvals"]
        self.assertEqual(len(pending), 2)
        first = pending[0]
        rejected = client.post(
            "/api/v1/workspaces/"+workspace["id"]+"/agent/tasks/"+task_id+
            "/steps/"+first["step_id"]+"/decision",
            headers=self.headers, json={"approved":False,"note":"Not approved."})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["task_status"], "blocked")
        remaining = client.get("/api/v1/approvals?workspace_id="+workspace["id"],
                               headers=self.headers).json()["approvals"]
        self.assertEqual(remaining, [])


    def test_agent_task_can_be_cancelled_and_pending_approvals_are_closed(self):
        client = TestClient(create_app(Path(self.temp.name)/"cancel.sqlite3",
                                       router=FakeToolPlanningRouter(), token="test-secret"))
        workspaces = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"]
        workspace = next(w for w in workspaces if w["name"] == "SovereignOS")
        created = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/plan",
                              headers=self.headers,
                              json={"goal":"Create and verify a local Markdown draft in this workspace."})
        task_id = created.json()["task"]["id"]
        cancelled = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/tasks/"+task_id+"/cancel",
                                headers=self.headers)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        pending = client.get("/api/v1/approvals?workspace_id="+workspace["id"],
                             headers=self.headers).json()["approvals"]
        self.assertEqual(pending, [])
        detail = client.get("/api/v1/workspaces/"+workspace["id"]+"/agent/tasks/"+task_id,
                            headers=self.headers).json()
        self.assertEqual(len(detail["approval_events"]), 1)
        self.assertEqual(detail["approval_events"][0]["decision"], "cancelled")
        execute = client.post("/api/v1/workspaces/"+workspace["id"]+"/agent/tasks/"+task_id+"/execute-next",
                              headers=self.headers)
        self.assertEqual(execute.status_code, 409)


    def test_agent_planner_requires_server_verified_cloud_consent(self):
        router = FakeCloudOnlyRouter()
        client = TestClient(create_app(Path(self.temp.name)/"cloud-consent.sqlite3",
                                       router=router, token="test-secret"))
        workspaces = client.get("/api/v1/workspaces", headers=self.headers).json()["workspaces"]
        workspace = next(w for w in workspaces if w["name"] == "SovereignOS")
        path = "/api/v1/workspaces/"+workspace["id"]+"/agent/plan"
        denied = client.post(path, headers=self.headers,
                             json={"goal":"Prepare a safe plan for a project milestone."})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(router.calls, 0)
        allowed = client.post(path, headers=self.headers,
                              json={"goal":"Prepare a safe plan for a project milestone.",
                                    "allow_cloud_processing":True})
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(router.calls, 1)

if __name__ == "__main__":
    unittest.main()


