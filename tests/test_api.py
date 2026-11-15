"""REST API authentication, workspace isolation and IoT safety tests."""
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from sovereign_core.api import create_app

class FakeRouter:
    configuration_warnings = []
    def health_snapshot(self):
        return [{"provider":"Test","model":"fake","runtime":"cloud","status":"ready",
                 "cooldown_reason":"","failures":0,"successes":0}]
    def invoke(self, messages):
        class Reply:
            content = "Test response"
        class Info:
            provider = "Test"
            model = "fake"
        return Reply(), Info(), 0

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
    def test_model_health_comes_from_router(self):
        data=self.client.get("/api/v1/models/health",headers=self.headers).json()
        self.assertEqual(data["providers"][0]["provider"],"Test")
    def test_iot_data_is_marked_simulated(self):
        data=self.client.get("/api/v1/iot/devices",headers=self.headers).json()
        self.assertTrue(data["simulation"])
        self.assertTrue(all(x["status"]=="simulation" for x in data["devices"]))
    def test_physical_device_commands_blocked(self):
        result=self.client.post("/api/v1/iot/commands",headers=self.headers,
                                json={"command":"open_valve"})
        self.assertEqual(result.status_code,403)

if __name__ == "__main__":
    unittest.main()
