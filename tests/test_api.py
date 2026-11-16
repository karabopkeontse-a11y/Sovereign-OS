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
    def invoke(self, messages):
        class Reply:
            content = "Test response"
        class Info:
            provider = "Test"
            model = "fake"
        return Reply(), Info(), 0

@unittest.skipIf(TestClient is None, "Install requirements-api.txt to run REST API tests")
class SovereignAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.temp.name)/"api.sqlite3",
                                             router=FakeRouter(), token="test-secret"))
        self.headers = {"Authorization":"Bearer test-secret"}
    def tearDown(self):
        self.temp.cleanup()
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

if __name__ == "__main__":
    unittest.main()
