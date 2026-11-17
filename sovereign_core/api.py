"""Authenticated REST API shared by SovereignOS desktop, web and Android clients."""
from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os, secrets, sqlite3
from pathlib import Path
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv
from .workspaces import WorkspaceService
from .specialists import specialist_for, specialist_prompt

# Load local provider credentials from the project root without overriding
# environment variables explicitly supplied by the launching process.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT / ".env.local", override=False)

def now():
    return datetime.now(timezone.utc).isoformat()

class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = None
    voice_mode: bool = False
    images: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("images")
    @classmethod
    def validate_images(cls, values):
        for value in values:
            if not value.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
                raise ValueError("Images must be PNG, JPEG or WebP data URLs.")
            if len(value) > 6 * 1024 * 1024:
                raise ValueError("Each image must be 4 MB or smaller.")
        return values

class GroupChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = None
    specialist_ids: list[str] = Field(min_length=2, max_length=4)

class ModelSelectInput(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=160)

class CommandInput(BaseModel):
    command: str = Field(min_length=1, max_length=120)
    parameters: dict = Field(default_factory=dict)

class SovereignAPI:
    def __init__(self, db_path, router=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspaces = WorkspaceService(self.db_path)
        self.workspaces.ensure_default_workspaces()
        self.router = router
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS api_conversations (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_api_conversations_workspace
                    ON api_conversations(workspace_id, updated_at);
                CREATE TABLE IF NOT EXISTS api_messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, provider TEXT, model TEXT, created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES api_conversations(id)
                );
            """)

    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def get_router(self):
        if self.router is None:
            from .model_router import ModelRouter
            self.router = ModelRouter(max_tokens=int(os.getenv("SOVEREIGN_MAX_OUTPUT_TOKENS", "800")))
        return self.router

    def chat(self, workspace_id, payload):
        try:
            workspace = self.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        db = self.connection()
        try:
            conversation_id = payload.conversation_id
            if conversation_id:
                row = db.execute("SELECT id FROM api_conversations WHERE id=? AND workspace_id=?",
                                 (conversation_id, workspace.id)).fetchone()
                if row is None:
                    raise HTTPException(404, "Conversation not found in this workspace")
            else:
                conversation_id = str(uuid4())
                stamp = now()
                db.execute("INSERT INTO api_conversations VALUES (?, ?, ?, ?, ?)",
                           (conversation_id, workspace.id, payload.message[:72], stamp, stamp))
                db.commit()
            prior = db.execute("SELECT role, content FROM api_messages WHERE conversation_id=? "
                               "ORDER BY created_at DESC LIMIT 12", (conversation_id,)).fetchall()
        finally:
            db.close()
        memories = self.workspaces.search(workspace.id, payload.message, limit=5)
        memory_context = "\n".join("- " + item.content for item in memories)
        assistant_name = "Thothi, the customer-facing SmartFlow BW assistant powered by SovereignOS" if workspace.name == "SmartFlow BW" else "SovereignOS"
        system = ("You are " + assistant_name + " in the " + workspace.name + " workspace. Purpose: "
                  + workspace.purpose + ". Use only this workspace context. Never claim to execute "
                  "external actions. Financial, publishing, destructive and physical-device actions "
                  "require human approval. Relevant memories:\n" + (memory_context or "(none)"))
        if payload.voice_mode:
            system += " For voice conversations, respond naturally and concisely, like a helpful person on a live call. Use short spoken sentences, avoid Markdown, tables and decorative symbols, and lead with the useful answer."
        try:
            from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
            messages = [SystemMessage(content=system)]
            for item in reversed(prior):
                messages.append(HumanMessage(content=item["content"]) if item["role"] == "user"
                                else AIMessage(content=item["content"]))
            router = self.get_router()
            if payload.images:
                selected = router.candidates[router.active_index] if router.candidates else None
                selected_model = selected.info.model.casefold() if selected else ""
                if not any(marker in selected_model for marker in ("vision", "-vl", ":vl")):
                    raise HTTPException(409, "Image understanding needs an active vision model. Select OpenRouter Free · nvidia/nemotron-3.5-super-vl:free in Model health, then try again.")
                content = [{"type": "text", "text": payload.message}]
                content.extend({"type": "image_url", "image_url": {"url": image}} for image in payload.images)
                messages.append(HumanMessage(content=content))
            else:
                messages.append(HumanMessage(content=payload.message))
            response, info, used_index = (router.invoke(messages, max_tokens=320) if payload.voice_mode else router.invoke(messages))
            reply = response.content if hasattr(response, "content") else str(response)
            provider, model = info.provider, info.model
        except Exception as exc:
            raise HTTPException(503, "Model unavailable: " + type(exc).__name__ + ": " + str(exc))
        db = self.connection()
        try:
            stamp = now()
            db.execute("INSERT INTO api_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid4()), conversation_id, "user", payload.message + (" [Image attached]" if payload.images else ""), None, None, stamp))
            db.execute("INSERT INTO api_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid4()), conversation_id, "assistant", reply, provider, model, stamp))
            db.execute("UPDATE api_conversations SET updated_at=? WHERE id=?", (stamp, conversation_id))
            db.commit()
        finally:
            db.close()
        health = router.health_snapshot() if hasattr(router, "health_snapshot") else []
        selected = next((item.get("model") for item in health if item.get("active")), None)
        return {"conversation_id": conversation_id, "workspace_id": workspace.id,
                "reply": reply, "provider": provider, "model": model,
                "fallback_used": used_index != getattr(router, "active_index", used_index),
                "selected_model": selected}

def create_app(db_path=None, router=None, token=None):
    state = SovereignAPI(db_path or os.getenv("SOVEREIGN_DB_PATH",
                          "sovereign_workspace/sovereign_tasks.sqlite3"), router)
    expected_token = token if token is not None else os.getenv("SOVEREIGN_API_TOKEN", "")
    @asynccontextmanager
    async def lifespan(app):
        app.state.sovereign = state
        yield
    app = FastAPI(title="SovereignOS API", version="1.0.0", lifespan=lifespan)
    origins = [x.strip() for x in os.getenv("SOVEREIGN_API_CORS_ORIGINS", "").split(",") if x.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                           allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
    def authorize(authorization: str | None = Header(default=None)):
        if not expected_token:
            raise HTTPException(503, "API locked: configure SOVEREIGN_API_TOKEN")
        supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if not secrets.compare_digest(supplied, expected_token):
            raise HTTPException(401, "Invalid or missing bearer token")
        return True
    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "service": "SovereignOS API", "version": "1.0.0"}
    @app.get("/api/v1/workspaces", dependencies=[Depends(authorize)])
    def list_workspaces():
        return {"workspaces": [{"id": w.id, "name": w.name, "purpose": w.purpose}
                                for w in state.workspaces.list_workspaces()]}
    @app.get("/api/v1/workspaces/{workspace_id}/conversations", dependencies=[Depends(authorize)])
    def list_conversations(workspace_id: str):
        try:
            state.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        db = state.connection()
        try:
            rows = db.execute("SELECT id, workspace_id, title, created_at, updated_at "
                              "FROM api_conversations WHERE workspace_id=? ORDER BY updated_at DESC",
                              (workspace_id,)).fetchall()
            return {"conversations": [dict(row) for row in rows]}
        finally:
            db.close()
    @app.get("/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages", dependencies=[Depends(authorize)])
    def conversation_messages(workspace_id: str, conversation_id: str):
        try:
            state.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        db = state.connection()
        try:
            owner = db.execute("SELECT id FROM api_conversations WHERE id=? AND workspace_id=?", (conversation_id, workspace_id)).fetchone()
            if owner is None:
                raise HTTPException(404, "Conversation not found in this workspace")
            rows = db.execute("SELECT role, content, provider, model, created_at FROM api_messages WHERE conversation_id=? ORDER BY created_at, id", (conversation_id,)).fetchall()
            return {"messages": [dict(row) for row in rows]}
        finally:
            db.close()
    @app.post("/api/v1/workspaces/{workspace_id}/chat", dependencies=[Depends(authorize)])
    def chat(workspace_id: str, payload: ChatInput):
        return state.chat(workspace_id, payload)
    @app.get("/api/v1/models/specialists", dependencies=[Depends(authorize)])
    def model_specialists():
        router_instance = state.get_router()
        providers = router_instance.health_snapshot()
        return {"specialists": [
            {**specialist_for(index, candidate),
             "status": providers[index].get("status", "connected"),
             "active": providers[index].get("active", False)}
            for index, candidate in enumerate(getattr(router_instance, "candidates", []))
        ], "note": "Specialists are roles assigned to configured models. No additional provider is silently enabled."}

    @app.post("/api/v1/workspaces/{workspace_id}/group-chat", dependencies=[Depends(authorize)])
    def group_chat(workspace_id: str, payload: GroupChatInput):
        try:
            workspace = state.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        router_instance = state.get_router()
        candidates = getattr(router_instance, "candidates", [])
        specialists = [specialist_for(i, c) for i, c in enumerate(candidates)]
        selected = [next((sp for sp in specialists if sp["id"] == sid), None)
                    for sid in payload.specialist_ids]
        if any(item is None for item in selected):
            raise HTTPException(409, "One or more selected Specialists are no longer configured. Refresh Model Health.")
        db = state.connection()
        try:
            conversation_id = payload.conversation_id
            if conversation_id:
                owner = db.execute("SELECT id FROM api_conversations WHERE id=? AND workspace_id=?",
                                   (conversation_id, workspace.id)).fetchone()
                if owner is None:
                    raise HTTPException(404, "Conversation not found in this workspace")
            else:
                conversation_id = str(uuid4())
                stamp = now()
                db.execute("INSERT INTO api_conversations VALUES (?, ?, ?, ?, ?)",
                           (conversation_id, workspace.id, payload.message[:72], stamp, stamp))
            prior = db.execute("SELECT role, content FROM api_messages WHERE conversation_id=? "
                               "ORDER BY created_at DESC LIMIT 12", (conversation_id,)).fetchall()
            stamp = now()
            db.execute("INSERT INTO api_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid4()), conversation_id, "user", payload.message, None, None, stamp))
            db.commit()
        finally:
            db.close()
        try:
            from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
            memories = state.workspaces.search(workspace.id, payload.message, limit=5)
            memory_context = "\n".join("- " + item.content for item in memories) or "(none)"
            base_messages = []
            for item in reversed(prior):
                base_messages.append(HumanMessage(content=item["content"]) if item["role"] == "user"
                                     else AIMessage(content=item["content"]))
            base_messages.append(HumanMessage(content=payload.message))
            results = []
            for specialist in selected:
                system = (specialist_prompt(specialist, workspace.name) +
                          "\nWorkspace purpose: " + workspace.purpose +
                          "\nRelevant workspace memories:\n" + memory_context)
                messages = [SystemMessage(content=system), *base_messages]
                response, info, used_index = router_instance.invoke(
                    messages, start_index=specialist["index"])
                reply = response.content if hasattr(response, "content") else str(response)
                results.append({"specialist": specialist["name"], "role": specialist["role"],
                                "provider": info.provider, "model": info.model, "reply": reply})
                base_messages.append(AIMessage(content="[" + specialist["name"] + "] " + str(reply)))
                db = state.connection()
                try:
                    db.execute("INSERT INTO api_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (str(uuid4()), conversation_id, "assistant",
                                "[" + specialist["name"] + "] " + str(reply),
                                info.provider, info.model, now()))
                    db.execute("UPDATE api_conversations SET updated_at=? WHERE id=?",
                               (now(), conversation_id))
                    db.commit()
                finally:
                    db.close()
        except Exception as exc:
            raise HTTPException(503, "Group session incomplete: " + type(exc).__name__ + ": " + str(exc))
        return {"conversation_id": conversation_id, "workspace_id": workspace.id,
                "responses": results, "specialists": [item["id"] for item in selected]}

    @app.get("/api/v1/models/health", dependencies=[Depends(authorize)])
    def model_health():
        router_instance = state.get_router()
        return {"providers": router_instance.health_snapshot(),
                "warnings": router_instance.configuration_warnings,
                "active_model": next(({"provider": p["provider"], "model": p["model"]}
                    for p in router_instance.health_snapshot() if p.get("active")), None),
                "configuration_mode": "backend-environment"}
    @app.post("/api/v1/models/select", dependencies=[Depends(authorize)])
    def select_model(payload: ModelSelectInput):
        router_instance = state.get_router()
        try:
            selected = router_instance.select_model(payload.provider, payload.model)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"selected": selected, "note": "Manual selection saved for this API session. Automatic fallback remains enabled."}
    @app.get("/api/v1/iot/devices", dependencies=[Depends(authorize)])
    def iot_devices():
        return {"simulation": True, "devices": [
            {"id": "demo-water-01", "name": "Water tank sensor", "kind": "water_level",
             "status": "simulation", "unit": "%", "value": 62},
            {"id": "demo-power-01", "name": "Power monitor", "kind": "energy",
             "status": "simulation", "unit": "kWh", "value": 1.8}]}
    @app.get("/api/v1/iot/devices/{device_id}/telemetry", dependencies=[Depends(authorize)])
    def iot_telemetry(device_id: str):
        samples = {"demo-water-01": {"unit": "%", "points": [58, 60, 61, 60, 62]}, "demo-power-01": {"unit": "kWh", "points": [1.2, 1.4, 1.5, 1.7, 1.8]}}
        if device_id not in samples:
            raise HTTPException(404, "Device not found")
        return {"device_id": device_id, "simulation": True, **samples[device_id]}
    @app.get("/api/v1/approvals", dependencies=[Depends(authorize)])
    def approvals():
        return {"approvals": [], "note": "Remote approval queue is not configured."}
    @app.post("/api/v1/iot/commands", dependencies=[Depends(authorize)])
    def iot_command(payload: CommandInput):
        raise HTTPException(403, "Physical commands disabled until a trusted gateway, device identity and human approval are configured.")
    return app

app = create_app()
