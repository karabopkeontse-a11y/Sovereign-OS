"""Authenticated REST API shared by SovereignOS desktop, web and Android clients."""
from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import asyncio, io, json
import os, secrets, sqlite3, shutil, subprocess, zipfile
import time
from pathlib import Path
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv
from .workspaces import WorkspaceService
from .specialists import specialist_for, specialist_prompt
from .agent_tools import TOOLS as AGENT_TOOLS, execute_workspace_tool, validate_tool_input, verify_workspace_tool, workspace_root

# Load local provider credentials from the project root without overriding
# environment variables explicitly supplied by the launching process.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT / ".env.local", override=False)

def now():
    return datetime.now(timezone.utc).isoformat()

class VoiceInput(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    rate: float = Field(default=1.14, ge=0.75, le=1.2)


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

class AgentPlanInput(BaseModel):
    goal: str = Field(min_length=10, max_length=4000)


class AgentDecisionInput(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=1000)


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
                               "ORDER BY created_at DESC LIMIT " + ("6" if payload.voice_mode else "12"), (conversation_id,)).fetchall()
        finally:
            db.close()
        memories = self.workspaces.search(workspace.id, payload.message, limit=2 if payload.voice_mode else 5)
        memory_context = "\n".join("- " + item.content for item in memories)
        assistant_name = "Thothi, the customer-facing SmartFlow BW assistant powered by SovereignOS" if workspace.name == "SmartFlow BW" else "SovereignOS"
        system = ("You are " + assistant_name + " in the " + workspace.name + " workspace. Purpose: "
                  + workspace.purpose + ". Use only this workspace context. Never claim to execute "
                  "external actions. Financial, publishing, destructive and physical-device actions "
                  "require human approval. Relevant memories:\n" + (memory_context or "(none)"))
        if payload.voice_mode:
            system += " For voice conversations, optimise for a fast live exchange. Give the direct answer first in one or two short sentences, then pause for the user. Avoid long preambles, Markdown, tables and decorative symbols. Ask at most one concise follow-up question when essential."
        try:
            from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
            messages = [SystemMessage(content=system)]
            for item in reversed(prior):
                messages.append(HumanMessage(content=item["content"]) if item["role"] == "user"
                                else AIMessage(content=item["content"]))
            router = self.get_router()
            if payload.images:
                content = [{"type": "text", "text": payload.message}]
                content.extend({"type": "image_url", "image_url": {"url": image}} for image in payload.images)
                messages.append(HumanMessage(content=content))
            else:
                messages.append(HumanMessage(content=payload.message))
            model_started = time.perf_counter()
            response, info, used_index = router.invoke(
                messages,
                max_tokens=192 if payload.voice_mode else None,
                require_vision=bool(payload.images),
            )
            model_latency_ms = round((time.perf_counter() - model_started) * 1000)
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
                "selected_model": selected, "runtime": info.runtime,
                "model_latency_ms": model_latency_ms}


def create_libreoffice_test_document(workspace_root: Path):
    """Create one fixed, harmless ODT inside the dedicated agent workspace."""
    workspace_root = Path(workspace_root).resolve()
    target_dir = workspace_root / "agent_workspace" / "libreoffice"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir = target_dir.resolve()
    try:
        target_dir.relative_to(workspace_root)
    except ValueError as exc:
        raise RuntimeError("The LibreOffice test folder resolves outside the SovereignOS agent workspace.") from exc
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = (target_dir / f"sovereignos-libreoffice-test-{stamp}.odt").resolve()
    if target_dir not in target.parents:
        raise RuntimeError("The test document path escaped the agent workspace.")
    content = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">
 <office:body><office:text>
  <text:h text:outline-level="1">SovereignOS · LibreOffice integration test</text:h>
  <text:p>This document was created locally by SovereignOS.</text:p>
  <text:p>Purpose: verify that the desktop agent can create a document in its own workspace and open it in LibreOffice.</text:p>
  <text:p>Safety boundary: this test did not read, edit, upload, or share any existing personal files.</text:p>
  <text:p>Next step: approve a specific file or folder before granting any broader document access.</text:p>
 </office:text></office:body>
</office:document-content>"""
    manifest = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">
 <manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>"""
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        archive.writestr("content.xml", content, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("META-INF/manifest.xml", manifest, compress_type=zipfile.ZIP_DEFLATED)

    candidates = [shutil.which("soffice"), shutil.which("libreoffice")]
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.getenv(variable)
            if base:
                candidates.extend([
                    str(Path(base) / "LibreOffice" / "program" / "soffice.exe"),
                    str(Path(base) / "Programs" / "LibreOffice" / "program" / "soffice.exe"),
                ])
    executable = next((item for item in candidates if item and Path(item).is_file()), None)
    if not executable:
        raise FileNotFoundError(
            "The test document was created in the SovereignOS agent workspace, "
            "but LibreOffice was not found. Install LibreOffice or add soffice to PATH."
        )
    process = subprocess.Popen(
        [executable, "--writer", "--norestore", "--nodefault", str(target)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        shell=False, close_fds=(os.name != "nt"),
    )
    return {"file_name": target.name, "opened": True, "process_id": process.pid,
            "scope": "SovereignOS agent workspace only"}

def create_app(db_path=None, router=None, token=None):
    state = SovereignAPI(db_path or os.getenv("SOVEREIGN_DB_PATH",
                          "sovereign_workspace/sovereign_tasks.sqlite3"), router)
    expected_token = token if token is not None else os.getenv("SOVEREIGN_API_TOKEN", "")
    @asynccontextmanager
    async def lifespan(app):
        app.state.sovereign = state
        yield
    app = FastAPI(title="SovereignOS API", version="1.0.0", lifespan=lifespan)
    origins = [x.strip() for x in os.getenv("SOVEREIGN_API_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080").split(",") if x.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                           allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "X-Sovereign-Language"])
    def authorize(authorization: str | None = Header(default=None)):
        if not expected_token:
            raise HTTPException(503, "API locked: configure SOVEREIGN_API_TOKEN")
        supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if not secrets.compare_digest(supplied, expected_token):
            raise HTTPException(401, "Invalid or missing bearer token")
        return True
    @app.get("/api/v1/bootstrap")
    def local_bootstrap(request: Request):
        # Only the local SovereignOS web clients may obtain the ephemeral session token.
        client_host = request.client.host if request.client else ""
        origin = request.headers.get("origin", "")
        allowed_origins = {
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:8080", "http://127.0.0.1:8080",
        }
        configured = {x.strip() for x in os.getenv("SOVEREIGN_API_CORS_ORIGINS", "").split(",") if x.strip()}
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(403, "Local bootstrap is only available on this computer.")
        if not origin or origin not in (allowed_origins | configured):
            raise HTTPException(403, "A valid approved local SovereignOS browser origin is required for bootstrap.")
        return {"api_url": "http://127.0.0.1:" + str(int(os.getenv("SOVEREIGN_API_PORT", "8765"))),
                "token": expected_token, "mode": "local-desktop"}


    @app.get("/api/v1/voice/status", dependencies=[Depends(authorize)])
    def voice_status():
        ffmpeg_available = shutil.which("ffmpeg") is not None
        try:
            import speech_recognition  # noqa: F401
            recognition_available = True
        except ImportError:
            recognition_available = False
        try:
            import edge_tts  # noqa: F401
            speech_output_available = True
        except ImportError:
            speech_output_available = False
        available = ffmpeg_available and recognition_available
        if not ffmpeg_available:
            reason = "FFmpeg is not installed or is not on PATH. Voice capture cannot be converted for transcription."
        elif not recognition_available:
            reason = "The SpeechRecognition package is not installed in the API environment."
        elif not speech_output_available:
            reason = "Voice transcription is ready. Browser speech output will be used because Edge TTS is unavailable."
        else:
            reason = "Voice dependencies are available. Google Speech Recognition requires an internet connection."
        return {"available": available, "ffmpeg_available": ffmpeg_available,
                "recognition_available": recognition_available,
                "speech_output_available": speech_output_available,
                "provider": "Google Speech Recognition with browser voice or Edge TTS fallback",
                "reason": reason}

    @app.post("/api/v1/voice/synthesize", dependencies=[Depends(authorize)])
    async def synthesize_voice(payload: VoiceInput):
        text = payload.text.strip()
        if not text:
            raise HTTPException(422, "Speech text cannot be empty.")
        try:
            import edge_tts

            rate_percent = round((payload.rate - 1.0) * 100)
            communicate = edge_tts.Communicate(
                text,
                "en-US-AriaNeural",
                rate=f"{rate_percent:+d}%"
            )
            audio_parts = []
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    audio_parts.append(chunk["data"])

            audio = b"".join(audio_parts)
            if not audio:
                raise HTTPException(502, "Speech provider returned no audio.")

            return Response(
                content=audio,
                media_type="audio/mpeg",
                headers={
                    "Cache-Control": "no-store, private",
                    "Pragma": "no-cache",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                502,
                "Speech generation failed: " + type(exc).__name__
            ) from exc


    @app.post("/api/v1/voice/transcribe", dependencies=[Depends(authorize)])
    async def transcribe_voice(request: Request):
        """Transcribe a short in-memory audio clip; never persist the recording."""
        started = time.perf_counter()
        max_audio_bytes = 8 * 1024 * 1024

        content_type = request.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        allowed_types = {
            "audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg",
            "audio/wav", "audio/x-wav", "audio/aac", "audio/flac",
            "audio/3gpp", "audio/3gpp2",
        }
        if media_type not in allowed_types:
            raise HTTPException(415, "Unsupported audio format.")

        declared_length = request.headers.get("content-length")
        if declared_length:
            try:
                if int(declared_length) > max_audio_bytes:
                    raise HTTPException(413, "Audio upload exceeds the 8 MB limit.")
            except ValueError:
                raise HTTPException(400, "Invalid Content-Length header.")

        audio_bytes = bytearray()
        async for chunk in request.stream():
            audio_bytes.extend(chunk)
            if len(audio_bytes) > max_audio_bytes:
                raise HTTPException(413, "Audio upload exceeds the 8 MB limit.")

        if not audio_bytes:
            raise HTTPException(422, "No audio was received.")

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise HTTPException(503, "The local audio conversion engine is unavailable.")

        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0",
                "-t", "25",
                "-vn", "-ac", "1", "-ar", "16000",
                "-f", "wav", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            wav_bytes, ffmpeg_error = await asyncio.wait_for(
                process.communicate(bytes(audio_bytes)),
                timeout=15,
            )
        except asyncio.TimeoutError:
            if "process" in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            raise HTTPException(504, "Audio conversion timed out.")
        except OSError:
            raise HTTPException(503, "Could not start the local audio conversion engine.")

        if process.returncode != 0 or not wav_bytes:
            raise HTTPException(422, "The received audio could not be decoded.")

        try:
            import speech_recognition as sr

            recognizer = sr.Recognizer()
            with sr.AudioFile(io.BytesIO(wav_bytes)) as source:
                audio_data = recognizer.record(source)
        except Exception:
            raise HTTPException(422, "The converted audio could not be read.")

        language = request.headers.get("x-sovereign-language", "en-GB").strip()
        allowed_languages = {"en-GB", "en-US", "en-ZA", "tn-BW"}
        if language not in allowed_languages:
            raise HTTPException(400, "Unsupported voice recognition language.")
        try:
            transcript = await asyncio.wait_for(
                asyncio.to_thread(
                    recognizer.recognize_google,
                    audio_data,
                    language=language,
                ),
                timeout=15,
            )
        except sr.UnknownValueError:
            raise HTTPException(422, "Speech could not be understood. Please try again.")
        except sr.RequestError:
            raise HTTPException(502, "The speech recognition service is unavailable.")
        except asyncio.TimeoutError:
            raise HTTPException(504, "Speech recognition timed out.")

        transcript = transcript.strip()
        if not transcript:
            raise HTTPException(422, "No speech was detected.")

        return {
            "transcript": transcript,
            "provider": "Google Speech Recognition",
            "processing_ms": round((time.perf_counter() - started) * 1000),
        }

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "service": "SovereignOS API", "version": "1.0.0"}
    @app.post("/api/v1/desktop/libreoffice/test", dependencies=[Depends(authorize)])
    def libreoffice_test():
        try:
            result = create_libreoffice_test_document(state.db_path.parent)
            return {"status": "opened", **result}
        except FileNotFoundError as exc:
            raise HTTPException(503, str(exc))
        except Exception as exc:
            raise HTTPException(500, "LibreOffice test failed: " + type(exc).__name__ + ": " + str(exc))

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
    @app.post("/api/v1/workspaces/{workspace_id}/agent/plan", dependencies=[Depends(authorize)])
    def create_agent_plan(workspace_id: str, payload: AgentPlanInput):
        try:
            workspace = state.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            planning_prompt = (
                "You are the planning component of SovereignOS. Convert the user's goal into a "
                "practical, ordered plan. Return ONLY valid JSON with keys title, description, steps. "
                "title must be concise. description must restate the goal. steps must contain 2 to 8 "
                "objects with action, tool_name, arguments and verification_criterion. tool_name may be null. "
                "The ONLY available tools are workspace.list_files (arguments: {}), workspace.read_file "
                "(arguments: {\"path\":\"relative/path.md\"}), and workspace.create_note "
                "(arguments: {\"filename\":\"draft.md\",\"content\":\"draft text\"}). Use list_files "
                "or read_file only inside the selected workspace agent folder. Use create_note only to create "
                "a new Markdown draft in that folder; it requires approval and never overwrites. Set tool_name "
                "to null when no listed tool fits. Never invent a path, shell command, code, script, network "
                "request, publishing action, payment, transfer, deletion or physical control. Include "
                "verification_criterion for every tool step. The server assigns permissions independently."
            )
            planner_router = state.get_router()
            candidate_indices = None
            if hasattr(planner_router, "candidates"):
                # Never engage Qwen or dynamic free-model aliases in the agent planner.
                candidate_indices = [
                    index for index, candidate in enumerate(planner_router.candidates)
                    if "qwen" not in candidate.info.model.casefold()
                    and "qwen" not in candidate.info.provider.casefold()
                    and candidate.info.model.casefold() not in {"openrouter/free", "auto"}
                ]
                if not candidate_indices:
                    raise HTTPException(
                        409,
                        "Agent planning is paused: configure an explicitly named non-Qwen model first. Qwen remains offline."
                    )
            response, info, used_index = planner_router.invoke(
                [SystemMessage(content=planning_prompt),
                 HumanMessage(content="Workspace: " + workspace.name + "\nGoal: " + payload.goal)],
                max_tokens=900,
                candidate_indices=candidate_indices,
            )
            raw = response.content if hasattr(response, "content") else str(response)
            if isinstance(raw, list):
                raw = "\n".join(str(item.get("text", "")) if isinstance(item, dict) else str(item)
                                 for item in raw)
            raw = str(raw).strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                if raw[:4].casefold() == "json":
                    raw = raw[4:].strip()
            plan = json.loads(raw)
            if not isinstance(plan, dict):
                raise ValueError("The planner response must be a JSON object.")
            title_value = plan.get("title")
            description_value = plan.get("description", payload.goal)
            if not isinstance(title_value, str) or not isinstance(description_value, str):
                raise ValueError("The planner returned invalid task metadata.")
            title = title_value.strip()[:120]
            description = description_value.strip()[:2000]
            proposed = plan.get("steps")
            if not title or not isinstance(proposed, list) or not 2 <= len(proposed) <= 8:
                raise ValueError("The planner returned an incomplete plan.")
            actions = []
            for item in proposed:
                if not isinstance(item, dict):
                    raise ValueError("Every planned step must be a structured object.")
                action_value = item.get("action")
                if not isinstance(action_value, str):
                    raise ValueError("The planner returned a step without a valid action.")
                action = action_value.strip()
                if not action or len(action) > 500:
                    raise ValueError("The planner returned an invalid step.")
                tool_name = item.get("tool_name")
                arguments = item.get("arguments", {})
                criterion = item.get("verification_criterion", "")
                if not isinstance(criterion, str) or len(criterion) > 500:
                    raise ValueError("The planner returned an invalid verification criterion.")
                if tool_name is not None:
                    if not isinstance(tool_name, str):
                        raise ValueError("The planner returned an invalid tool name.")
                    arguments = validate_tool_input(tool_name, arguments)
                    permission = AGENT_TOOLS[tool_name]
                    if not criterion.strip():
                        criterion = "Verify the registered workspace tool completed within its scope."
                else:
                    if arguments not in ({}, None):
                        raise ValueError("A step without a registered tool cannot contain tool arguments.")
                    lowered = action.casefold()
                    sensitive = (
                        "delete", "remove", "publish", "send ", "share", "upload",
                        "transfer", "pay ", "purchase", "buy ", "submit", "deploy",
                        "execute", "unlock", "open valve", "control device", "email ",
                        "post ", "external", "irreversible",
                    )
                    read_only = (
                        "research", "read ", "list ", "search", "summarize", "summarise",
                        "analyze", "analyse", "inspect", "review", "compare", "check ",
                        "verify", "calculate", "identify", "map ", "outline",
                    )
                    if any(term in lowered for term in sensitive):
                        permission = "high_impact"
                    elif lowered.startswith(read_only):
                        permission = "read_only"
                    else:
                        permission = "external_effect"
                    arguments = {}
                actions.append((action, permission, tool_name, arguments, criterion))
            tasks = state.workspaces.tasks
            problem = tasks.create_problem(workspace.id, title, description)
            tasks.set_problem_status(problem.id, "diagnosing")
            steps = []
            for index, (action, permission, tool_name, arguments, criterion) in enumerate(actions, 1):
                step = tasks.add_plan_step(problem.id, index, action, permission)
                if tool_name:
                    tasks.add_step_spec(step.id, tool_name,
                                        json.dumps(arguments, ensure_ascii=False),
                                        criterion)
                steps.append(step)
            tasks.set_problem_status(problem.id, "planned")
            if any(step.status == "awaiting_approval" for step in steps):
                tasks.set_problem_status(problem.id, "awaiting_approval")
            return {"task": tasks.get_problem(problem.id).__dict__,
                    "steps": [step.__dict__ for step in steps],
                    "planner": {"provider": info.provider, "model": info.model},
                    "execution_started": False,
                    "note": "Plan saved. No tools or external actions have been executed."}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Could not create a validated agent plan: " + type(exc).__name__ + ": " + str(exc))

    @app.get("/api/v1/workspaces/{workspace_id}/agent/tasks", dependencies=[Depends(authorize)])
    def list_agent_tasks(workspace_id: str):
        try:
            workspace = state.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        tasks = state.workspaces.tasks
        result = []
        for problem in tasks.list_problems(workspace.id):
            result.append({"task": problem.__dict__,
                           "steps": [step.__dict__ for step in tasks.list_steps(problem.id)],
                           "tool_specs": tasks.list_step_specs(problem.id),
                           "approval_events": tasks.list_approval_events(workspace.id, problem.id)})
        return {"tasks": result}

    @app.get("/api/v1/workspaces/{workspace_id}/agent/tasks/{problem_id}", dependencies=[Depends(authorize)])
    def get_agent_task(workspace_id: str, problem_id: str):
        try:
            workspace = state.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        problem = state.workspaces.tasks.get_problem(problem_id)
        if not problem or problem.workspace_id != workspace.id:
            raise HTTPException(404, "Task not found in this workspace")
        tasks = state.workspaces.tasks
        return {"task": problem.__dict__,
                "steps": [step.__dict__ for step in tasks.list_steps(problem.id)],
                "tool_specs": tasks.list_step_specs(problem.id),
                "approval_events": tasks.list_approval_events(workspace.id, problem.id)}

    @app.post("/api/v1/workspaces/{workspace_id}/agent/tasks/{problem_id}/execute-next", dependencies=[Depends(authorize)])
    def execute_next_agent_step(workspace_id: str, problem_id: str):
        try:
            workspace = state.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        tasks = state.workspaces.tasks
        problem = tasks.get_problem(problem_id)
        if not problem or problem.workspace_id != workspace.id:
            raise HTTPException(404, "Task not found in this workspace")
        if problem.status in {"blocked", "failed", "completed", "cancelled"}:
            raise HTTPException(409, "This task is not in an executable state.")
        selected = None
        spec = None
        for candidate in tasks.list_steps(problem.id):
            candidate_spec = tasks.get_step_spec(candidate.id)
            if candidate_spec and candidate.status in {"planned", "approved"}:
                selected, spec = candidate, candidate_spec
                break
        if selected is None or spec is None:
            raise HTTPException(409, "No registered tool step is ready. Review pending approvals or add a supported tool step.")
        if selected.permission_level != "read_only" and selected.status != "approved":
            raise HTTPException(403, "This tool step requires explicit approval before execution.")
        started_at = None
        started_clock = None
        run_recorded = False
        arguments = {}
        input_summary = "{}"
        try:
            arguments = json.loads(spec["input_json"])
            input_summary = json.dumps({
                key: (f"[{len(value)} characters]" if key == "content" and isinstance(value, str) else value)
                for key, value in arguments.items()
            }, ensure_ascii=False)
            root = workspace_root(state.db_path, workspace.id)
            if problem.status in {"planned", "awaiting_approval"}:
                tasks.set_problem_status(problem.id, "running")
            tasks.update_step_status(selected.id, "running")
            started_at = now()
            started_clock = time.perf_counter()
            result = execute_workspace_tool(spec["tool_name"], arguments, root)
            passed, criterion, evidence = verify_workspace_tool(
                spec["tool_name"], arguments, result, root
            )
            tasks.update_step_status(selected.id, "verifying")
            tasks.record_tool_run(
                selected.id, spec["tool_name"], input_summary,
                (f"Listed {result.get('count')} workspace files" if spec["tool_name"] == "workspace.list_files"
                 else f"Read {result.get('path')} ({result.get('bytes')} bytes)" if spec["tool_name"] == "workspace.read_file"
                 else f"Created {result.get('path')} ({result.get('bytes')} bytes)"),
                "success" if passed else "verification_failed",
                started_at, now(),
            )
            run_recorded = True
            tasks.record_verification(
                selected.id, spec["verification_criterion"] or criterion,
                "server-side workspace boundary and read-back check",
                passed, evidence,
            )
            if not passed:
                current = tasks.get_problem(problem.id)
                if current and current.status == "running":
                    tasks.set_problem_status(problem.id, "failed")
                raise HTTPException(500, "The tool ran, but its verification check failed.")
            remaining = tasks.list_steps(problem.id)
            if remaining and all(item.status == "verified" for item in remaining):
                tasks.complete_problem(problem.id)
            output = dict(result)
            if isinstance(output.get("content"), str) and len(output["content"]) > 20000:
                output["content"] = output["content"][:20000]
                output["truncated"] = True
            return {"task_id": problem.id, "step": tasks.get_step(selected.id).__dict__,
                    "tool_result": output, "verified": True,
                    "duration_ms": round((time.perf_counter() - started_clock) * 1000),
                    "task_status": tasks.get_problem(problem.id).status}
        except HTTPException:
            raise
        except Exception as exc:
            if started_at and not run_recorded:
                try:
                    tasks.record_tool_run(
                        selected.id, spec["tool_name"], input_summary,
                        "Tool failed: " + type(exc).__name__, "failed", started_at, now()
                    )
                except Exception:
                    pass
            current_step = tasks.get_step(selected.id)
            if current_step and current_step.status in {"running", "verifying"}:
                try:
                    if current_step.status == "running":
                        tasks.update_step_status(selected.id, "failed")
                    else:
                        tasks.record_verification(
                            selected.id, spec["verification_criterion"],
                            "Execution error", False, type(exc).__name__
                        )
                except Exception:
                    pass
            current_problem = tasks.get_problem(problem.id)
            if current_problem and current_problem.status == "running":
                try:
                    tasks.set_problem_status(problem.id, "failed")
                except ValueError:
                    pass
            raise HTTPException(500, "Agent tool failed safely: " + type(exc).__name__ + ": " + str(exc))

    @app.post("/api/v1/workspaces/{workspace_id}/agent/tasks/{problem_id}/cancel", dependencies=[Depends(authorize)])
    def cancel_agent_task(workspace_id: str, problem_id: str):
        try:
            workspace = state.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        tasks = state.workspaces.tasks
        problem = tasks.get_problem(problem_id)
        if not problem or problem.workspace_id != workspace.id:
            raise HTTPException(404, "Task not found in this workspace")
        if problem.status in {"completed", "cancelled"}:
            raise HTTPException(409, "This task is already closed.")
        steps = tasks.list_steps(problem.id)
        if any(step.status in {"running", "verifying"} for step in steps):
            raise HTTPException(409, "A tool step is currently running. Wait for it to finish before cancelling.")
        for step in steps:
            if step.status in {"planned", "awaiting_approval", "approved", "blocked"}:
                tasks.update_step_status(step.id, "cancelled")
        cancelled = tasks.set_problem_status(problem.id, "cancelled")
        return {"task_id": problem.id, "status": cancelled.status,
                "pending_approvals_cancelled": True, "execution_started": False}

    @app.post("/api/v1/workspaces/{workspace_id}/agent/tasks/{problem_id}/steps/{step_id}/decision", dependencies=[Depends(authorize)])
    def decide_agent_step(workspace_id: str, problem_id: str, step_id: str,
                          payload: AgentDecisionInput):
        try:
            workspace = state.workspaces.get_workspace(workspace_id)
        except ValueError:
            raise HTTPException(404, "Workspace not found")
        tasks = state.workspaces.tasks
        problem = tasks.get_problem(problem_id)
        if not problem or problem.workspace_id != workspace.id:
            raise HTTPException(404, "Task not found in this workspace")
        step = next((item for item in tasks.list_steps(problem.id) if item.id == step_id), None)
        if not step:
            raise HTTPException(404, "Plan step not found in this task")
        if problem.status in {"blocked", "failed", "completed", "cancelled"}:
            raise HTTPException(409, "This task is closed to further approval decisions.")
        if step.status != "awaiting_approval":
            raise HTTPException(409, "This step is no longer awaiting approval.")
        decision = "approved" if payload.approved else "rejected"
        try:
            tasks.update_step_status(step.id, "approved" if payload.approved else "cancelled")
            event = tasks.record_approval_event(workspace.id, problem.id, step.id,
                                                decision, payload.note)
            remaining = [item for item in tasks.list_steps(problem.id)
                         if item.status == "awaiting_approval"]
            if not payload.approved:
                # A rejected step closes the task and cancels its other pending requests.
                for pending_step in remaining:
                    tasks.update_step_status(pending_step.id, "cancelled")
                current = tasks.get_problem(problem.id)
                if current and current.status in {"planned", "awaiting_approval", "running"}:
                    tasks.set_problem_status(problem.id, "blocked")
            elif not remaining and problem.status == "awaiting_approval":
                tasks.set_problem_status(problem.id, "planned")
            return {"decision": event, "task_status": tasks.get_problem(problem.id).status,
                    "execution_started": False}
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/v1/approvals", dependencies=[Depends(authorize)])
    def approvals(workspace_id: str | None = None):
        db = state.connection()
        try:
            sql = ("SELECT s.id AS step_id, s.step_number, s.action, s.permission_level, "
                   "s.status AS step_status, p.id AS problem_id, p.title AS task_title, "
                   "p.description AS task_description, p.status AS task_status, "
                   "p.workspace_id, w.name AS workspace_name, sp.tool_name, sp.input_json "
                   "FROM plan_steps s JOIN problems p ON p.id=s.problem_id "
                   "JOIN workspaces w ON w.id=p.workspace_id "
                   "LEFT JOIN agent_step_specs sp ON sp.step_id=s.id "
                   "WHERE s.status='awaiting_approval'")
            params = []
            if workspace_id:
                try:
                    state.workspaces.get_workspace(workspace_id)
                except ValueError:
                    raise HTTPException(404, "Workspace not found")
                sql += " AND p.workspace_id=?"
                params.append(workspace_id)
            sql += " ORDER BY p.created_at, s.step_number"
            rows = db.execute(sql, params).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                raw_arguments = item.pop("input_json", None)
                details = {}
                if raw_arguments:
                    try:
                        arguments = json.loads(raw_arguments)
                    except (TypeError, ValueError):
                        arguments = {}
                    if item.get("tool_name") == "workspace.create_note":
                        details = {"filename": arguments.get("filename", ""),
                                   "content": arguments.get("content", "")}
                    elif item.get("tool_name") == "workspace.read_file":
                        details = {"path": arguments.get("path", "")}
                item["requested_details"] = details
                items.append(item)
            return {"approvals": items,
                    "note": "Approval decisions are persisted. Approval does not itself execute an action."}
        finally:
            db.close()
    @app.post("/api/v1/iot/commands", dependencies=[Depends(authorize)])
    def iot_command(payload: CommandInput):
        raise HTTPException(403, "Physical commands disabled until a trusted gateway, device identity and human approval are configured.")
    return app

app = create_app()
