"""
Sovereign OS — Ultimate Edition
================================
Single-file rewrite of the "Sovereign OS" desktop assistant.

WHAT THIS FILE FIXES (see the numbered sections below):
  1. Thread safety ("slot stack empty")
  2. Visible persona dropdown + dynamic status indicator
  3. Animated CSS avatar (idle / listening / thinking / talking / happy)
  4. Clickable Recent Files -> glass-morphism modal (Refine with AI / Download / Close),
     now rendering images/video/docx/pdf, not just plain text
  5. Instant vanilla-JS drag & drop media preview, auto-fit + double-click full view
  6. Real window controls via native=False

LATEST ROUND OF CHANGES:
  - Mic recording can now be stopped mid-way (tap the mic again) instead of waiting
    for silence detection, and the transcript lands in the input box for you to review
    and edit before sending — nothing from voice is auto-sent anymore.
  - save_file / read_file / list_files / convert_file are now location-aware:
    workspace, desktop, documents, downloads. This is a deliberate, small allow-list,
    not arbitrary filesystem access — see SAVE_LOCATIONS and the note below.
  - convert_file (agent tool) and the modal's format picker (UI) both convert between
    .txt/.md/.docx/.pdf using the same write_text_as_format() function.
  - Chat history now persists to sovereign_chat_history.json and reloads on startup;
    a "Clear chat" button in the header wipes it.
  - LLM calls, transcription, and TTS now retry with backoff on transient failures —
    genuine help for a weak/flaky connection.

ON "SAVE ANYWHERE" AND SECURITY: you asked for Sovereign to write files "directly into
my computer" with "proper constraint and security awareness." What's implemented is a
NAMED allow-list (workspace/desktop/documents/downloads) that the model picks from by
name — not a free-text path the model invents, and not elevated/admin access, which
was deliberately left out. Existing files are never overwritten (auto-renamed instead).
This is a considered scope decision, not an oversight — see the chat response this
file was delivered with for the full reasoning.

ON OFFLINE USE: the LLM (Groq), speech-to-text (Google), and text-to-speech (Microsoft)
are all cloud calls — none of them can run with zero internet, and no local model
fits the original 4GB RAM target. What's implemented here is resilience to a WEAK
connection (retries with backoff on all three), plus clear error messages instead of
a crash when a call fails outright. True offline AI would need a different, heavier
architecture — flag this if you want to go there next.

NEW DEPENDENCIES this round: fpdf2 (PDF writing) — see the install command.

IMPORTANT — please read:
  This is a clean rewrite based on your description of the app, not an edit of your
  original file. I don't have access to your machine or C:\\SAFE\\AGENTIC AI, so I
  could not literally read your existing code — only what you described. The STT,
  TTS, tool-calling, and persona logic below are working, from-scratch implementations
  that follow the same libraries you listed. If your original file has custom tools,
  persona text, or a specific "recent files" data source, port that logic into the
  clearly marked spots (PERSONAS, TOOLS, WORKSPACE_DIR).

  Groq deprecated `qwen/qwen3.6-27b` on 2026-09-14. Sovereign now defaults to its
  direct successor, `qwen/qwen3.8-27b`. An old value in the environment file is
  automatically migrated to the successor.
"""

import os
import re
import json
import time
import asyncio
import threading
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv, dotenv_values

import pyaudio
import speech_recognition as sr
import edge_tts
from pydub import AudioSegment
from pydub.playback import play as pydub_play

from nicegui import ui, run, app

from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

from sovereign_core.memory import MemoryStore
from sovereign_core.workspaces import WorkspaceService
from sovereign_core.model_router import ModelRouter, ModelRoutingError, ModelInfo

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Load credentials from the project folder, not merely the shell's current
# working directory. Supports the common .env name and the user's existing
# example.env / .enc filenames. Environment variables already set in Windows
# take precedence over values stored in files.
BASE_DIR = Path(__file__).resolve().parent
ENV_CANDIDATES = [BASE_DIR / ".env", BASE_DIR / "example.env", BASE_DIR / ".enc", BASE_DIR / ".env.txt", BASE_DIR / "example.env.txt", BASE_DIR / ".env.example"]
ENV_FILE = next(
    (candidate for candidate in ENV_CANDIDATES
     if candidate.is_file() and any(
         dotenv_values(candidate).get(key)
         for key in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "OLLAMA_MODEL")
     )),
    None,
)
if ENV_FILE is not None:
    load_dotenv(dotenv_path=ENV_FILE, override=False)
else:
    # Still load any standard .env discovered by python-dotenv, then report
    # a precise setup message below if no key is available.
    load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

MODEL_NAME = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b").strip()
# Automatically migrate the retired model ID if it remains in an existing .env file.
if MODEL_NAME == "qwen/qwen3.6-27b":
    MODEL_NAME = "qwen/qwen3.8-27b"
os.environ["GROQ_MODEL"] = MODEL_NAME
MAX_TOKENS = int(os.getenv("SOVEREIGN_MAX_OUTPUT_TOKENS", "800"))
DEFAULT_VOICE = "en-US-AriaNeural"

WORKSPACE_DIR = BASE_DIR / "sovereign_workspace"
WORKSPACE_DIR.mkdir(exist_ok=True)
WORKSPACE_DB = WORKSPACE_DIR / "sovereign_tasks.sqlite3"
WORKSPACE_SERVICE = WorkspaceService(WORKSPACE_DB)
WORKSPACES = WORKSPACE_SERVICE.ensure_default_workspaces()
MEMORY_STORE = WORKSPACE_SERVICE.memory  # retained for legacy vault operations
CHAT_HISTORY_DIR = WORKSPACE_DIR / "chat_history"
CHAT_HISTORY_DIR.mkdir(exist_ok=True)
app.add_static_files("/workspace-files", str(WORKSPACE_DIR))  # lets the modal show/download images & videos directly

# Deliberately a small, named allow-list rather than an arbitrary path the model could
# pick — "save it to my Desktop" works, but nothing can write to arbitrary system paths.
SAVE_LOCATIONS = {
    "workspace": WORKSPACE_DIR,
    "desktop": Path.home() / "Desktop",
    "documents": Path.home() / "Documents",
    "downloads": Path.home() / "Downloads",
}

CHAT_HISTORY_FILE = BASE_DIR / "sovereign_chat_history.json"

TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".csv", ".log", ".js", ".html", ".css", ".yaml", ".yml"}
DOCX_EXTENSIONS = {".docx"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}

if not any(WORKSPACE_DIR.iterdir()):
    (WORKSPACE_DIR / "welcome.md").write_text(
        "# Welcome to Sovereign OS\n\n"
        "Files placed in this folder show up in the Recent Files panel.\n"
        "Click a file to preview it, refine it with AI, or download it.\n"
        "Point WORKSPACE_DIR at a different folder if you'd rather track files elsewhere.\n",
        encoding="utf-8",
    )

# Replace with your original persona set if it differs — this is a reasonable
# generic default since I don't have your original PERSONAS dict.
PERSONAS = {
    "General Assistant": "You are Sovereign, a helpful, concise desktop AI assistant. Be direct and practical.",
    "Coder": "You are Sovereign, an expert pair-programmer. Give precise, working code and briefly explain trade-offs.",
    "Creative Writer": "You are Sovereign, an imaginative creative-writing partner. Favor vivid, original prose.",
    "Researcher": "You are Sovereign, a careful research assistant. Use the web_search tool for anything time-sensitive "
                  "or factual you're unsure of, and explain findings in plain language.",
}
DEFAULT_PERSONA = "General Assistant"


# ─────────────────────────────────────────────────────────────────────────────
# RELIABILITY HELPERS — weak/flaky connections shouldn't mean instant failure
# ─────────────────────────────────────────────────────────────────────────────

def _with_retries(fn, *args, attempts: int = 3, base_delay: float = 1.5, **kwargs):
    """Retries a blocking call with short backoff. Helps with weak/flaky internet;
    does nothing for a genuinely offline machine — see the module docstring."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if i < attempts - 1:
                time.sleep(base_delay * (i + 1))
    raise last_exc


async def _with_retries_async(coro_fn, *args, attempts: int = 3, base_delay: float = 1.5, **kwargs):
    last_exc = None
    for i in range(attempts):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if i < attempts - 1:
                await asyncio.sleep(base_delay * (i + 1))
    raise last_exc


def _unique_path(path: Path) -> Path:
    """Never silently overwrite an existing file — 'report.pdf' becomes
    'report (1).pdf' if 'report.pdf' is already there."""
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


# ─────────────────────────────────────────────────────────────────────────────
# TOOLS
# ─────────────────────────────────────────────────────────────────────────────

@tool
def web_search(query: str) -> str:
    """Search the live web for current facts, recent events, sources and general research.
    Uses several free DDGS backends one at a time so one blocked provider does not disable all search."""
    from ddgs import DDGS
    errors = []
    # Keep each backend isolated: some providers rate-limit or block certain networks.
    for backend in ("auto", "bing", "brave", "google", "duckduckgo", "mojeek", "yahoo"):
        try:
            with DDGS(timeout=12) as ddgs:
                results = list(ddgs.text(query, max_results=5, backend=backend))
            if results:
                lines = [f"Web search results for: {query} (provider: {backend})"]
                for r in results:
                    lines.append(
                        f"- {r.get('title', 'Untitled')}: {r.get('body', r.get('description', ''))} "
                        f"({r.get('href', r.get('url', ''))})"
                    )
                return "\n".join(lines)
            errors.append(f"{backend}: no results")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{backend}: {type(exc).__name__}: {exc}")
    return "All free web-search backends failed. Details: " + " | ".join(errors[-7:])


@tool
def get_weather(location: str) -> str:
    """Fetch current conditions and a 7-day forecast for a named place.
    Uses Open-Meteo geocoding and forecast APIs (no API key for non-commercial use).
    For known Botswana towns, a country restriction is applied to prevent resolving
    the same town name in another country. Reports the provider's model timestamp."""
    def fetch_json(url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "SovereignOS/1.1 (weather lookup)"})
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        raw_location = (location or "").strip()
        if not raw_location:
            return "Weather lookup needs a town or city name."

        # Botswana town names that users commonly ask for without repeating the country.
        bw_places = {
            "gaborone", "maunatlala", "maun", "francistown", "kasane", "palapye",
            "serowe", "jwaneng", "lobatse", "selebi-phikwe", "selebi phikwe",
            "molepolole", "tshabong", "ghanzi", "ghantsi", "letlhakane", "orapa",
            "sowa", "sowa town", "ramotswa", "mochudi", "tati siding", "shakawe",
            "nata", "bobonong", "kanye", "masunga", "tonota", "rakops"
        }
        normalized = raw_location.casefold().strip()
        country_code = "BW" if ("botswana" in normalized or normalized in bw_places) else None
        # Strip a trailing country label for the geocoder's name field.
        search_name = re.sub(r",?\s*botswana\s*$", "", raw_location, flags=re.I).strip()
        geo_params = {"name": search_name, "count": 10, "language": "en", "format": "json"}
        if country_code:
            geo_params["countryCode"] = country_code
        geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(geo_params)
        geo = fetch_json(geo_url)
        matches = geo.get("results") or []
        if country_code:
            matches = [m for m in matches if m.get("country_code", "").upper() == country_code]
        if not matches:
            return (f"I couldn't verify '{raw_location}' in the requested country. "
                    "Please check the spelling or provide the district/country.")

        target_name = search_name.split(",")[0].strip().casefold()
        exact = [m for m in matches if m.get("name", "").casefold() == target_name]
        place = exact[0] if exact else matches[0]
        # If the user explicitly named a country, never silently return a different one.
        if "botswana" in normalized and place.get("country_code", "").upper() != "BW":
            return f"Location verification failed: '{raw_location}' did not resolve to Botswana."

        forecast_params = {
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,rain,showers,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,weather_code",
            "forecast_days": 7, "timezone": "auto", "temperature_unit": "celsius"
        }
        forecast_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(forecast_params)
        data = fetch_json(forecast_url)
        cur, day = data.get("current", {}), data.get("daily", {})
        descriptions = {
            0:"clear sky",1:"mainly clear",2:"partly cloudy",3:"overcast",45:"fog",48:"depositing rime fog",
            51:"light drizzle",53:"moderate drizzle",55:"dense drizzle",61:"slight rain",63:"moderate rain",
            65:"heavy rain",71:"slight snow",73:"moderate snow",75:"heavy snow",80:"rain showers",
            81:"moderate rain showers",82:"violent rain showers",95:"thunderstorm",96:"thunderstorm with hail",
            99:"thunderstorm with heavy hail"
        }
        def weather_label(code):
            return descriptions.get(code, f"weather code {code}")

        name = place.get("name", search_name)
        admin = place.get("admin1", "")
        country = place.get("country", "")
        where = ", ".join(x for x in (name, admin, country) if x)
        timezone = data.get("timezone", "local time")
        current_time = cur.get("time", "not supplied by provider")
        lines = [
            f"Verified location: {where} (coordinates: {place['latitude']}, {place['longitude']})",
            f"Provider: Open-Meteo; timezone: {timezone}",
            f"Current model time: {current_time}",
            f"Current conditions: {cur.get('temperature_2m', 'unavailable')}°C; feels like {cur.get('apparent_temperature', 'unavailable')}°C; {weather_label(cur.get('weather_code'))}.",
            f"Humidity: {cur.get('relative_humidity_2m', 'unavailable')}%; wind: {cur.get('wind_speed_10m', 'unavailable')} km/h; precipitation: {cur.get('precipitation', 'unavailable')} mm.",
            "7-day daily forecast:"
        ]
        dates = day.get("time", [])
        for i, date in enumerate(dates):
            def at(key):
                values = day.get(key, [])
                return values[i] if i < len(values) else "unavailable"
            lines.append(
                f"- {date}: low {at('temperature_2m_min')}°C, high {at('temperature_2m_max')}°C, "
                f"{weather_label(at('weather_code'))}, rain probability {at('precipitation_probability_max')}%, "
                f"precipitation {at('precipitation_sum')} mm."
            )
        lines.append("Data note: current values are Open-Meteo's latest model-based conditions, not a guaranteed direct station observation. Do not invent or alter these values.")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        return f"Weather service failed for '{location}': {type(exc).__name__}: {exc}. No weather values are available; do not guess."


def _safe_filename(name: str) -> str:
    """Collapse any path separators/illegal characters so a saved file can never land
    outside WORKSPACE_DIR, regardless of what the model puts in `filename`."""
    name = (name or "").strip().replace("\\", "_").replace("/", "_")
    name = re.sub(r'[<>:"|?*]', "_", name).lstrip(".")
    return name or "untitled.txt"


@tool
def save_file(filename: str, content: str, location: str = "workspace") -> str:
    """Save text content to a file. `location` must be one of: workspace, desktop,
    documents, downloads (defaults to workspace). Use the workspace default unless the
    user explicitly asks to save somewhere on their computer — e.g. "save this to my
    desktop" -> location="desktop". `filename` should include an extension such as
    .txt or .md. Never overwrites an existing file — automatically renames instead."""
    try:
        base = SAVE_LOCATIONS.get(location, WORKSPACE_DIR)
        base.mkdir(parents=True, exist_ok=True)
        path = _unique_path(base / _safe_filename(filename))
        path.write_text(content, encoding="utf-8")
        return f"Saved to '{path.name}' in {location}."
    except Exception as exc:  # noqa: BLE001 - a bad save must never crash the chat
        return f"Save failed: {exc}"


TOOLS = [web_search, get_weather, save_file]


@tool
def list_files(location: str = "workspace") -> str:
    """List files in a location: workspace, desktop, documents, or downloads
    (defaults to workspace)."""
    base = SAVE_LOCATIONS.get(location, WORKSPACE_DIR)
    if not base.exists():
        return f"'{location}' folder not found."
    files = sorted((f for f in base.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)[:50]
    if not files:
        return f"No files found in {location}."
    return "\n".join(f"- {f.name}" for f in files)


@tool
def read_file(filename: str, location: str = "workspace") -> str:
    """Read and return the text content of a file. `location` must be one of:
    workspace, desktop, documents, downloads (defaults to workspace). Supports .txt,
    .md, .py, .json, .docx, and .pdf. Use this when the user asks what a file says,
    asks you to summarize or discuss a saved file, or refers to something saved
    earlier — including files elsewhere on their computer, not just the workspace."""
    base = SAVE_LOCATIONS.get(location, WORKSPACE_DIR)
    path = base / _safe_filename(filename)
    if not path.exists():
        return f"No file named '{filename}' found in {location}."
    try:
        return extract_text(path)[:8000]  # bounded so one huge document can't blow the context
    except Exception as exc:  # noqa: BLE001 - a bad read must never crash the chat
        return f"Could not read file: {exc}"


@tool
def convert_file(filename: str, target_format: str, location: str = "workspace") -> str:
    """Convert an existing file to a different format and save the result alongside it
    in the same location. `target_format` must be one of: txt, md, docx, pdf.
    `location` must be one of: workspace, desktop, documents, downloads (defaults to
    workspace). Reads the source file's text (.txt/.md/.py/.docx/.pdf all work as the
    source), then writes it back out in the new format. Use this when the user asks to
    convert, export, or change a saved file's format."""
    base = SAVE_LOCATIONS.get(location, WORKSPACE_DIR)
    path = base / _safe_filename(filename)
    if not path.exists():
        return f"No file named '{filename}' found in {location}."
    try:
        text = extract_text(path)
        out_path = _unique_path(path.with_suffix("." + target_format.lower().lstrip(".")))
        write_text_as_format(text, out_path)
        return f"Saved as '{out_path.name}' in {location}."
    except Exception as exc:  # noqa: BLE001 - a bad conversion must never crash the chat
        return f"Conversion failed: {exc}"


TOOLS += [list_files, read_file, convert_file]


def build_workspace_memory_tools(workspace_id: str):
    """Create memory tools whose workspace is captured by the desktop session.
    The LLM never receives a workspace_id argument and cannot switch its scope."""
    WORKSPACE_SERVICE.get_workspace(workspace_id)

    @tool
    def remember_fact(content: str, kind: str = "fact", confidence: float = 1.0) -> str:
        """Save a durable fact only when the user explicitly asks Sovereign to remember it."""
        try:
            item = WORKSPACE_SERVICE.remember(
                workspace_id, content, kind=kind, source="explicit user memory request",
                confidence=confidence, verified=False,
            )
            return f"Saved memory {item.id} (revision {item.revision}, confidence {item.confidence:.2f})."
        except Exception as exc:
            return f"Memory save failed: {type(exc).__name__}: {exc}"

    @tool
    def search_memories(query: str, verified_only: bool = False) -> str:
        """Search saved memories in the currently selected workspace."""
        try:
            matches = WORKSPACE_SERVICE.search(workspace_id, query, limit=10, verified_only=verified_only)
            if not matches:
                return "No matching saved memories were found in this workspace."
            return "\n".join(
                f"- [{item.id}] ({item.kind}; source={item.source}; confidence={item.confidence:.2f}; verified={item.verified}) {item.content}"
                for item in matches
            )
        except Exception as exc:
            return f"Memory search failed: {type(exc).__name__}: {exc}"

    @tool
    def correct_memory(memory_id: str, corrected_content: str, confidence: float = 1.0) -> str:
        """Correct an existing memory in the selected workspace only, after explicit user instruction."""
        try:
            item = WORKSPACE_SERVICE.correct(
                workspace_id, memory_id, corrected_content,
                source="explicit user correction request", confidence=confidence,
            )
            return f"Corrected memory saved as {item.id}; previous memory {memory_id} is retained as superseded."
        except Exception as exc:
            return f"Memory correction failed: {type(exc).__name__}: {exc}"

    @tool
    def memory_revision_history(memory_id: str) -> str:
        """Show revision history for a memory in the selected workspace."""
        try:
            revisions = WORKSPACE_SERVICE.history(workspace_id, memory_id)
            if not revisions:
                return "No revision history found for that memory."
            return "\n".join(
                f"- revision {item.revision} at {item.changed_at}: {item.content} "
                f"(source={item.source}; confidence={item.confidence:.2f}; verified={item.verified}; status={item.status})"
                for item in revisions
            )
        except Exception as exc:
            return f"Memory history failed: {type(exc).__name__}: {exc}"

    @tool
    def forget_memory(memory_id: str) -> str:
        """Permanently delete a saved memory in the selected workspace after explicit user instruction."""
        try:
            return ("Memory and revision history permanently deleted."
                    if WORKSPACE_SERVICE.forget(workspace_id, memory_id)
                    else "No memory with that ID was found in this workspace.")
        except Exception as exc:
            return f"Memory deletion failed: {type(exc).__name__}: {exc}"

    @tool
    def list_saved_memories() -> str:
        """List saved memories in the selected workspace for review."""
        try:
            items = WORKSPACE_SERVICE.list_memories(workspace_id)
            if not items:
                return "There are no saved memories in this workspace."
            return "\n".join(
                f"- [{item.id}] ({item.kind}; confidence={item.confidence:.2f}; verified={item.verified}) {item.content}"
                for item in items
            )
        except Exception as exc:
            return f"Memory listing failed: {type(exc).__name__}: {exc}"

    return [remember_fact, search_memories, correct_memory,
            memory_revision_history, forget_memory, list_saved_memories]


# ─────────────────────────────────────────────────────────────────────────────
# LLM — manual tool-calling loop
# ─────────────────────────────────────────────────────────────────────────────
# NOTE: langchain.agents (create_tool_calling_agent / AgentExecutor) is deliberately
# NOT used here. LangChain v1 has been actively restructuring that module — helpers
# have been added, moved to langchain-classic, and removed again across recent point
# releases, which is what produced the ImportError. bind_tools()/tool_calls is the
# lower-level primitive underneath all of those helpers and isn't subject to that
# churn, so the loop below is built directly on it instead.

MODEL_ROUTER = ModelRouter(max_tokens=MAX_TOKENS, temperature=0.7)
MAX_TOOL_HOPS = 4


def _bounded_history(history: list) -> list:
    """Keep recent conversation within a small prompt budget before provider calls."""
    max_chars = max(2000, int(os.getenv("SOVEREIGN_CONTEXT_CHARS", "10000")))
    max_messages = max(2, int(os.getenv("SOVEREIGN_HISTORY_MESSAGES", "10")))
    selected = []
    used = 0
    for message in reversed(history[-max_messages:]):
        content = str(getattr(message, "content", ""))[:max_chars]
        if selected and used + len(content) > max_chars:
            break
        if len(content) > max_chars:
            content = content[-max_chars:]
        selected.append(type(message)(content=content))
        used += len(content)
    return list(reversed(selected))


def call_llm(persona_key: str, history: list, user_text: str,
             workspace_id: str) -> tuple[str, object]:
    """Run a workspace-bound agent turn with quota-aware model failover."""
    workspace = WORKSPACE_SERVICE.get_workspace(workspace_id)
    try:
        recalled = WORKSPACE_SERVICE.search(workspace_id, user_text, limit=5)
        memory_context = "\n".join(
            f"- {item.content} (source: {item.source}; confidence: {item.confidence:.2f}; verified: {item.verified})"
            for item in recalled
        )[:1500]
    except Exception:
        memory_context = ""

    persona_prompt = (
        PERSONAS.get(persona_key, PERSONAS[DEFAULT_PERSONA])
        + f"\n\nACTIVE WORKSPACE: {workspace.name}. All memory tools are already bound to this workspace. "
        "Never request or invent a workspace ID and never attempt to access another workspace. "
        "Use the supplied saved-memory context only when relevant. Do not claim a memory exists "
        "unless it appears in context or search_memories returns it. Only save, correct or delete "
        "memories when the user explicitly asks. Use memory_revision_history when asked how a "
        "saved memory changed. A saved memory is not automatically verified. "
        "\nSaved-memory context:\n" + (memory_context or "(No keyword-matched memories.)")
        + "\n\nLIVE DATA RULES: For current weather, call get_weather with the exact place the user specifies. "
        "Never substitute another town or country. For a known Botswana town named without a country, "
        "interpret it as Botswana. Preserve returned location, timestamp, units, forecast dates and source. "
        "Never invent weather values or claim model data is a station observation. For current events, "
        "recent facts, prices or general web research, call web_search. Report tool errors honestly."
    )
    messages = [SystemMessage(content=persona_prompt)] + _bounded_history(history) + [HumanMessage(content=user_text)]
    active_tools = TOOLS + build_workspace_memory_tools(workspace_id)
    tool_map = {item.name: item for item in active_tools}
    active_index = 0
    active_info = None
    fallback_used = False

    def reported_model():
        if active_info is None:
            return None
        return ModelInfo(active_info.provider, active_info.model, active_info.runtime, fallback_used)

    for _ in range(MAX_TOOL_HOPS):
        ai_message, active_info, active_index = MODEL_ROUTER.invoke(
            messages, tools=active_tools, start_index=active_index
        )
        fallback_used = fallback_used or active_index > 0
        messages.append(ai_message)
        calls = getattr(ai_message, "tool_calls", None)
        if not calls:
            return ai_message.content, reported_model()
        for call in calls:
            tool_fn = tool_map.get(call["name"])
            try:
                result = tool_fn.invoke(call["args"]) if tool_fn else f"Unknown tool: {call['name']}"
            except Exception as exc:  # noqa: BLE001
                result = f"Tool error: {type(exc).__name__}: {exc}"
            messages.append(ToolMessage(content=str(result)[:5000], tool_call_id=call["id"]))

    final_message, active_info, final_index = MODEL_ROUTER.invoke(
        messages, tools=None, start_index=active_index
    )
    fallback_used = fallback_used or final_index > 0
    return final_message.content, reported_model()


# ─────────────────────────────────────────────────────────────────────────────
# SPEECH-TO-TEXT (blocking — always run via run.io_bound)
# ─────────────────────────────────────────────────────────────────────────────
# Recording uses raw PyAudio rather than recognizer.listen()'s auto-stop-on-silence,
# specifically so it can be cancelled mid-recording — listen() is one opaque blocking
# call with no way to interrupt it early; a manual chunk-read loop can check a stop
# flag between chunks instead.

MIC_RATE = 16000
MIC_CHANNELS = 1
MIC_CHUNK = 1024
MIC_FORMAT = pyaudio.paInt16
MIC_SAMPLE_WIDTH = 2  # bytes — matches paInt16


def record_until_stopped(recording_state: dict, max_seconds: float = 120) -> bytes:
    """Records raw mic audio until recording_state['stop'] is set (mic button clicked
    again) or max_seconds is hit. recording_state is a plain dict checked from this
    background thread — a boolean flip is safe to read across threads with no extra
    locking needed for a single flag like this."""
    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(format=MIC_FORMAT, channels=MIC_CHANNELS, rate=MIC_RATE,
                          input=True, frames_per_buffer=MIC_CHUNK)
    except OSError as exc:
        pa.terminate()
        raise RuntimeError("No microphone detected — check your audio input device.") from exc

    frames = []
    max_chunks = int(MIC_RATE / MIC_CHUNK * max_seconds)
    try:
        for _ in range(max_chunks):
            if recording_state.get("stop"):
                break
            frames.append(stream.read(MIC_CHUNK, exception_on_overflow=False))
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()
    return b"".join(frames)


def transcribe_raw_audio(raw_bytes: bytes) -> str:
    if not raw_bytes:
        raise RuntimeError("Didn't hear anything — try again.")
    recognizer = sr.Recognizer()
    audio = sr.AudioData(raw_bytes, MIC_RATE, MIC_SAMPLE_WIDTH)
    last_exc = None
    for attempt in range(3):
        try:
            return recognizer.recognize_google(audio)
        except sr.UnknownValueError as exc:
            # retrying this never helps — the audio didn't change — so fail immediately
            raise RuntimeError("Couldn't understand that — please speak clearly.") from exc
        except sr.RequestError as exc:
            last_exc = exc  # network/service hiccup — worth retrying for weak connections
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Speech recognition service unavailable: {last_exc}")


# ─────────────────────────────────────────────────────────────────────────────
# TEXT-TO-SPEECH
# ─────────────────────────────────────────────────────────────────────────────

async def synthesize_speech(text: str, voice: str = DEFAULT_VOICE, stop_event=None):
    """Create speech plus word-level timing marks; observe cancellation between stream chunks."""
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    boundaries = []

    async def _save():
        boundaries.clear()
        communicate = edge_tts.Communicate(text, voice, rate="+18%", boundary="WordBoundary")
        with open(path, "wb") as audio_file:
            async for chunk in communicate.stream():
                if stop_event is not None and stop_event.is_set():
                    break
                if chunk["type"] == "audio":
                    audio_file.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    boundaries.append({
                        "offset": int(chunk["offset"]),
                        "duration": int(chunk["duration"]),
                        "text": chunk["text"],
                    })

    try:
        await _with_retries_async(_save)
        return path, boundaries
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def load_audio_file(path: str):
    """Decode before starting the avatar clock, so animation starts with playback."""
    return AudioSegment.from_file(path)


def play_audio_segment(segment, stop_event=None) -> None:
    """Play decoded audio in short chunks so a stop request is acted on quickly."""
    audio = pyaudio.PyAudio()
    stream = None
    try:
        stream = audio.open(
            format=audio.get_format_from_width(segment.sample_width),
            channels=segment.channels,
            rate=segment.frame_rate,
            output=True,
            frames_per_buffer=1024,
        )
        frame_bytes = segment.frame_width * 1024
        raw = segment.raw_data
        for start in range(0, len(raw), frame_bytes):
            if stop_event is not None and stop_event.is_set():
                break
            stream.write(raw[start:start + frame_bytes], exception_on_underflow=False)
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            stream.close()
        audio.terminate()


def remove_audio_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# WORKSPACE / RECENT FILES
# ─────────────────────────────────────────────────────────────────────────────

def list_recent_files(limit: int = 15):
    files = [f for f in WORKSPACE_DIR.iterdir() if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[:limit]


def _chat_history_path(workspace_id: str | None = None) -> Path:
    if not workspace_id:
        return CHAT_HISTORY_FILE
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", workspace_id)
    return CHAT_HISTORY_DIR / f"{safe_id}.json"


def load_chat_history(workspace_id: str | None = None) -> list:
    path = _chat_history_path(workspace_id)
    # Preserve the previous single-history file as General's first-run history.
    if not path.exists() and workspace_id:
        try:
            selected = WORKSPACE_SERVICE.get_workspace(workspace_id)
            if selected.name == "General" and CHAT_HISTORY_FILE.exists():
                path = CHAT_HISTORY_FILE
        except Exception:
            pass
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [
            HumanMessage(content=item["content"]) if item.get("role") == "user" else AIMessage(content=item["content"])
            for item in raw
        ]
    except Exception:  # noqa: BLE001
        return []


def save_chat_history(history: list, workspace_id: str | None = None) -> None:
    try:
        serializable = [
            {"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": m.content}
            for m in history
        ]
        _chat_history_path(workspace_id).write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def extract_text(path: Path) -> str:
    """Text preview/extraction for everything the modal or the read_file tool can open."""
    ext = path.suffix.lower()
    if ext in DOCX_EXTENSIONS:
        from docx import Document  # lazy: only needed if you actually open a .docx
        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    if ext in PDF_EXTENSIONS:
        from pypdf import PdfReader  # lazy: only needed if you actually open a .pdf
        return "\n\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8", errors="replace")


def write_text_as_format(text: str, out_path: Path) -> None:
    """Counterpart to extract_text — writes plain text out as .txt/.md/.docx/.pdf.
    This produces a genuinely new document in that format from the text given; it does
    NOT preserve original Word/PDF formatting, images, or layout, since we only ever
    hold the extracted text, not the original binary structure."""
    ext = out_path.suffix.lower()
    if ext in (".txt", ".md"):
        out_path.write_text(text, encoding="utf-8")
    elif ext == ".docx":
        from docx import Document  # lazy: only needed for docx output
        doc = Document()
        for line in text.split("\n"):
            doc.add_paragraph(line)
        doc.save(str(out_path))
    elif ext == ".pdf":
        from fpdf import FPDF  # lazy: only needed for pdf output (pip: fpdf2)
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Helvetica", size=11)
        for line in text.split("\n"):
            # core PDF fonts are Latin-1 only; swap unsupported characters rather than crash
            pdf.multi_cell(0, 6, line.encode("latin-1", "replace").decode("latin-1"))
        pdf.output(str(out_path))
    else:
        raise ValueError(f"Unsupported target format: {ext}")


def _js_str(s: str) -> str:
    """Escape text for safe embedding inside a single-quoted JS string literal."""
    return (s or "").replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ").replace("\r", "")


# ─────────────────────────────────────────────────────────────────────────────
# HEAD HTML — all CSS + vanilla JS lives here (kept as a plain string, not an
# f-string, so none of these braces need escaping)
# ─────────────────────────────────────────────────────────────────────────────

HEAD_HTML = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #10121B;
  --panel: rgba(255,255,255,0.045);
  --panel-border: rgba(255,255,255,0.10);
  --accent-gold: #E3B341;
  --accent-teal: #4FD1C5;
  --accent-rose: #E2725B;
  --text: #F5F3ED;
  --text-muted: #A9A6B5;
}
* { box-sizing: border-box; }
body {
  background: radial-gradient(circle at 15% 0%, #1a1d2c 0%, var(--bg) 55%) !important;
  color: var(--text);
  font-family: 'IBM Plex Sans', 'Segoe UI', sans-serif;
}
.nicegui-content { padding: 0 !important; }

.glass-panel {
  background: var(--panel);
  border: 1px solid var(--panel-border);
  border-radius: 18px;
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  box-shadow: 0 8px 32px rgba(0,0,0,0.35);
}

/* ---------- Header ---------- */
.app-header {
  align-items: center;
  gap: 20px;
  padding: 14px 24px;
  border-bottom: 1px solid var(--panel-border);
  background: rgba(255,255,255,0.02);
}
.app-title {
  font-family: 'Sora', sans-serif;
  font-weight: 600;
  font-size: 20px;
  letter-spacing: 0.02em;
  color: var(--text);
}
.app-title-accent { color: var(--accent-gold); }
.status-indicator {
  font-family: 'IBM Plex Sans', sans-serif;
  font-size: 14px;
  color: var(--text-muted);
  padding: 4px 12px;
  border-radius: 999px;
  background: rgba(255,255,255,0.05);
  border: 1px solid var(--panel-border);
  transition: transform 0.2s ease;
}
.status-pulse { transform: scale(1.08); }

/* ---------- Dropdown visibility fix (persona select + modal format picker) ---------- */
.persona-select, .persona-select .q-field__control, .persona-select .q-field__native,
.persona-select .q-field__input, .persona-select .q-field__label,
.persona-select .q-select__native,
.modal-format-select, .modal-format-select .q-field__control, .modal-format-select .q-field__native,
.modal-format-select .q-field__input, .modal-format-select .q-field__label,
.modal-format-select .q-select__native {
  color: var(--text) !important;
}
.persona-select .q-field__control, .modal-format-select .q-field__control {
  background: rgba(255,255,255,0.06) !important;
  border-color: var(--panel-border) !important;
}
.q-menu { background: #1B1E2E !important; color: var(--text) !important; }
.q-menu .q-item, .q-menu .q-item__label, .q-menu .q-item__section { color: var(--text) !important; }
.q-menu .q-item:hover, .q-menu .q-item--active { background: rgba(227,179,65,0.15) !important; }

/* ---------- Body layout ---------- */
.app-body { padding: 20px; gap: 20px; align-items: stretch; flex-wrap: nowrap; }
.chat-panel { flex: 1 1 60%; min-width: 360px; padding: 18px; height: calc(100vh - 110px); display: flex; flex-direction: column; }
.side-panel { flex: 1 1 34%; min-width: 300px; display: flex; flex-direction: column; gap: 16px; }

.chat-log { flex: 1; overflow-y: auto; padding-right: 6px; gap: 10px; }
.chat-bubble {
  max-width: 80%;
  padding: 10px 14px;
  border-radius: 14px;
  line-height: 1.45;
  white-space: pre-wrap;
}
.chat-bubble-user { align-self: flex-end; background: var(--accent-gold); color: #1a1508; border-bottom-right-radius: 4px; }
.chat-bubble-assistant { align-self: flex-start; background: rgba(255,255,255,0.07); border: 1px solid var(--panel-border); border-bottom-left-radius: 4px; }
.chat-bubble .spoken-word-active { color: #17130a; background: var(--accent-gold); border-radius: 4px; box-shadow: 0 0 10px rgba(227,179,65,0.25); }

.chat-input-row { gap: 8px; align-items: center; margin-top: 12px; }
.chat-input { flex: 1; }
.icon-button { background: var(--accent-gold) !important; color: #1a1508 !important; border-radius: 12px !important; }

/* ---------- Avatar ---------- */
.avatar-stage { position: relative; height: 210px; display: flex; align-items: center; justify-content: center; }
.avatar { position: relative; width: 180px; height: 180px; animation: avatar-bob 3s ease-in-out infinite; }
@keyframes avatar-bob { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }
.avatar-glow {
  position: absolute; inset: -10px; border-radius: 50%;
  background: radial-gradient(circle, rgba(227,179,65,0.25), transparent 70%);
  filter: blur(6px); transition: background 0.4s ease;
}
.avatar-head {
  position: relative; width: 150px; height: 150px; margin: 15px auto 0; border-radius: 50%;
  background: linear-gradient(155deg, #2b2f45, #1b1e2e);
  border: 2px solid rgba(255,255,255,0.12);
  box-shadow: 0 10px 30px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.08);
  transition: transform 0.4s ease;
}
.avatar-eye {
  position: absolute; top: 58px; width: 26px; height: 32px; background: var(--text);
  border-radius: 50% 50% 45% 45%;
  transition: height 0.15s ease, border-radius 0.2s ease, transform 0.2s ease, top 0.2s ease;
}
.avatar-eye-left { left: 38px; } .avatar-eye-right { right: 38px; }
.avatar-eye-shine { position: absolute; top: 5px; left: 5px; width: 8px; height: 8px; border-radius: 50%; background: #fff; opacity: 0.9; }
.avatar-blush { position: absolute; top: 95px; width: 18px; height: 10px; border-radius: 50%; background: rgba(226,114,91,0.35); }
.avatar-blush-left { left: 20px; } .avatar-blush-right { right: 20px; }
.avatar-mouth {
  position: absolute; top: 105px; left: 50%; transform: translateX(-50%);
  width: 22px; height: 8px; border-radius: 0 0 14px 14px; background: var(--accent-gold);
  transition: all 0.15s ease;
}
.avatar-thinking-dots { display: none; position: absolute; top: -8px; left: 50%; transform: translateX(-50%); gap: 4px; }
.avatar-thinking-dots span { width: 6px; height: 6px; border-radius: 50%; background: var(--accent-gold); opacity: 0.3; animation: dot-fade 1.4s infinite; }
.avatar-thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
.avatar-thinking-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes dot-fade { 0%, 100% { opacity: 0.2; transform: translateY(0); } 50% { opacity: 1; transform: translateY(-4px); } }

.avatar-idle .avatar-eye { animation: blink 4.5s ease-in-out infinite; }
@keyframes blink { 0%, 92%, 100% { transform: scaleY(1); } 96% { transform: scaleY(0.1); } }

.avatar-listening .avatar-glow { background: radial-gradient(circle, rgba(79,209,197,0.35), transparent 70%); animation: pulse-glow 1.2s ease-in-out infinite; }
.avatar-listening .avatar-eye { height: 34px; }
@keyframes pulse-glow { 0%, 100% { opacity: 0.6; } 50% { opacity: 1; } }

.avatar-thinking .avatar-head { animation: tilt 1.6s ease-in-out infinite; }
.avatar-thinking .avatar-eye { transform: translateX(4px); }
.avatar-thinking .avatar-thinking-dots { display: flex; }
@keyframes tilt { 0%, 100% { transform: rotate(0); } 50% { transform: rotate(4deg); } }

.avatar-talking .avatar-mouth { animation: talk 0.28s ease-in-out infinite; }
@keyframes talk { 0%, 100% { height: 8px; border-radius: 0 0 14px 14px; } 50% { height: 16px; border-radius: 0 0 20px 20px; } }

.avatar-happy .avatar-eye { height: 14px; top: 66px; border-radius: 0 0 50% 50% / 0 0 100% 100%; }
.avatar-happy .avatar-mouth { width: 34px; height: 18px; border-radius: 0 0 20px 20px; background: var(--accent-teal); }
.avatar-happy .avatar-head { animation: happy-bounce 0.5s ease; }
@keyframes happy-bounce { 0% { transform: scale(1); } 40% { transform: scale(1.08); } 100% { transform: scale(1); } }

/* ---------- Recent files ---------- */
.files-panel { padding: 16px; }
.panel-title { font-family: 'Sora', sans-serif; font-size: 15px; color: var(--text-muted); margin-bottom: 8px; }
.files-list { gap: 4px; max-height: 160px; overflow-y: auto; }
.file-row { align-items: center; gap: 10px; padding: 8px 10px; border-radius: 10px; cursor: pointer; transition: background 0.15s ease; }
.file-row:hover { background: rgba(227,179,65,0.12); }
.file-icon { color: var(--accent-gold); }
.file-name { color: var(--text); font-size: 14px; }
.files-empty { color: var(--text-muted); font-size: 13px; padding: 6px 2px; }

/* ---------- Drag & drop preview ---------- */
.drop-zone {
  border: 2px dashed var(--panel-border); border-radius: 16px; padding: 20px;
  text-align: center; color: var(--text-muted); font-size: 13px;
  transition: border-color 0.2s ease, background 0.2s ease;
}
.drop-zone-active { border-color: var(--accent-teal); background: rgba(79,209,197,0.08); }
.preview-container { margin-top: 10px; max-height: 220px; overflow: auto; display: flex; justify-content: center; }
.preview-media { max-width: 100%; max-height: 210px; object-fit: contain; border-radius: 12px; display: block; margin: 0 auto; cursor: zoom-in; }
.preview-text { white-space: pre-wrap; font-size: 12px; color: var(--text-muted); background: rgba(255,255,255,0.04); padding: 10px; border-radius: 10px; }

/* ---------- Glass modal ---------- */
.glass-modal {
  background: rgba(27,30,46,0.55) !important;
  border: 1px solid rgba(255,255,255,0.18) !important;
  border-radius: 20px !important;
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  min-width: 420px; max-width: 640px; padding: 8px;
}
.modal-title { font-family: 'Sora', sans-serif; font-size: 17px; margin-bottom: 8px; color: var(--text); }
.modal-textarea { width: 100%; min-height: 280px; }
.modal-media { max-width: 100%; max-height: 400px; object-fit: contain; border-radius: 12px; display: block; margin: 0 auto; cursor: zoom-in; }
.modal-actions { gap: 10px; margin-top: 12px; justify-content: flex-end; flex-wrap: wrap; }
.modal-format-select { width: 90px; }

/* ---------- Full-view lightbox (double-click any preview image/video) ---------- */
.lightbox-overlay {
  display: none; position: fixed; inset: 0; background: rgba(8,9,15,0.9); z-index: 10000;
  align-items: center; justify-content: center; cursor: zoom-out;
}
.lightbox-overlay.active { display: flex; }
.lightbox-overlay img, .lightbox-overlay video { max-width: 92vw; max-height: 92vh; border-radius: 10px; box-shadow: 0 20px 60px rgba(0,0,0,0.6); }

/* ---------- Toasts (non-blocking JS "alerts") ---------- */
.toast-container { position: fixed; bottom: 20px; right: 20px; display: flex; flex-direction: column; gap: 8px; z-index: 9999; }
.toast {
  padding: 10px 16px; border-radius: 12px; font-size: 13px; color: var(--text);
  background: rgba(27,30,46,0.9); border-left: 4px solid var(--accent-teal);
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  opacity: 0; transform: translateY(8px); animation: toast-in 0.25s ease forwards;
  max-width: 320px;
}
.toast-success { border-left-color: var(--accent-teal); }
.toast-info { border-left-color: var(--accent-gold); }
.toast-warning { border-left-color: var(--accent-gold); }
.toast-error { border-left-color: var(--accent-rose); }
.toast-out { animation: toast-out 0.3s ease forwards; }
@keyframes toast-in { to { opacity: 1; transform: translateY(0); } }
@keyframes toast-out { to { opacity: 0; transform: translateY(8px); } }

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 8px; }
</style>

<div id="toast-container" class="toast-container"></div>
<div id="lightbox-overlay" class="lightbox-overlay"></div>

<script>
window.setAvatarState = function(state) {
  var el = document.getElementById('avatar');
  if (el) { el.className = 'avatar avatar-' + state; }
};

window.setStatus = function(text) {
  var el = document.getElementById('status-indicator');
  if (el) {
    el.textContent = text;
    el.classList.add('status-pulse');
    setTimeout(function () { el.classList.remove('status-pulse'); }, 400);
  }
};

/* Word-level speech clock. Audio is played locally; these marks keep the
   visible words and avatar aligned to the actual audio timeline. */
window.startSpeechSync = function(boundaries, bubbleId) {
  var bubble = document.getElementById(bubbleId);
  window.setAvatarState('talking');
  window.setStatus('🟣 Speaking');
  if (!bubble || !boundaries || !boundaries.length) return;
  var raw = bubble.textContent || '';
  var cursor = 0;
  var html = '';
  var marks = [];
  function esc(value) {
    return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/\"/g, '&quot;').replace(/'/g, '&#39;');
  }
  boundaries.forEach(function (b, i) {
    var word = b.text || '';
    var at = word ? raw.indexOf(word, cursor) : -1;
    if (at < 0) return;
    html += esc(raw.slice(cursor, at));
    html += '<span class="spoken-word" data-word-index="' + i + '">' + esc(word) + '</span>';
    marks.push({offset: Number(b.offset) / 10000000, index: i});
    cursor = at + word.length;
  });
  html += esc(raw.slice(cursor));
  if (marks.length) bubble.innerHTML = html;
  var spans = bubble.querySelectorAll('.spoken-word');
  var start = performance.now();
  var last = -1;
  function tick(now) {
    var elapsed = (now - start) / 1000;
    var current = -1;
    for (var j = 0; j < marks.length; j++) {
      if (marks[j].offset <= elapsed) current = marks[j].index;
      else break;
    }
    if (current !== last) {
      spans.forEach(function (span) {
        span.classList.toggle('spoken-word-active', Number(span.dataset.wordIndex) === current);
      });
      last = current;
    }
    if (window.sovereignSpeechActive) window.sovereignSpeechFrame = requestAnimationFrame(tick);
  }
  window.sovereignSpeechActive = true;
  window.sovereignSpeechFrame = requestAnimationFrame(tick);
};
window.finishSpeechSync = function() {
  window.sovereignSpeechActive = false;
  if (window.sovereignSpeechFrame) cancelAnimationFrame(window.sovereignSpeechFrame);
  document.querySelectorAll('.spoken-word-active').forEach(function (el) {
    el.classList.remove('spoken-word-active');
  });
  window.setAvatarState('idle');
  window.setStatus('🟢 Online');
};

window.showToast = function(message, kind) {
  var container = document.getElementById('toast-container');
  if (!container) return;
  var toast = document.createElement('div');
  toast.className = 'toast toast-' + (kind || 'info');
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(function () {
    toast.classList.add('toast-out');
    setTimeout(function () { toast.remove(); }, 300);
  }, 3200);
};

/* Full-view lightbox — used by both the drop-zone preview and the Recent Files modal */
window.openLightbox = function(kind, src) {
  var overlay = document.getElementById('lightbox-overlay');
  if (!overlay) return;
  overlay.innerHTML = '';
  var el = document.createElement(kind === 'video' ? 'video' : 'img');
  el.src = src;
  if (kind === 'video') { el.controls = true; el.autoplay = true; }
  overlay.appendChild(el);
  overlay.classList.add('active');
};
document.addEventListener('click', function (e) {
  var overlay = document.getElementById('lightbox-overlay');
  if (overlay && overlay.classList.contains('active') && e.target === overlay) {
    overlay.classList.remove('active');
    overlay.innerHTML = '';
  }
});

/* Vanilla-JS drag & drop — client-side only, instant preview, no server round trip */
document.addEventListener('dragover', function (e) {
  var zone = e.target.closest ? e.target.closest('#drop-zone') : null;
  if (zone) { e.preventDefault(); zone.classList.add('drop-zone-active'); }
});
document.addEventListener('dragleave', function (e) {
  var zone = e.target.closest ? e.target.closest('#drop-zone') : null;
  if (zone) { zone.classList.remove('drop-zone-active'); }
});
document.addEventListener('drop', function (e) {
  var zone = e.target.closest ? e.target.closest('#drop-zone') : null;
  if (!zone) return;
  e.preventDefault();
  zone.classList.remove('drop-zone-active');
  var files = e.dataTransfer ? e.dataTransfer.files : null;
  if (!files || !files.length) return;
  var file = files[0];
  var preview = document.getElementById('preview-container');
  if (!preview) return;
  preview.innerHTML = '';
  var reader = new FileReader();
  if (file.type.indexOf('image/') === 0) {
    reader.onload = function (ev) {
      var img = document.createElement('img');
      img.src = ev.target.result;
      img.className = 'preview-media';
      img.addEventListener('dblclick', function () { window.openLightbox('image', ev.target.result); });
      preview.appendChild(img);
    };
    reader.readAsDataURL(file);
  } else if (file.type.indexOf('video/') === 0) {
    reader.onload = function (ev) {
      var video = document.createElement('video');
      video.src = ev.target.result;
      video.className = 'preview-media';
      video.controls = true; video.autoplay = true; video.muted = true; video.loop = true;
      video.addEventListener('dblclick', function () { window.openLightbox('video', ev.target.result); });
      preview.appendChild(video);
    };
    reader.readAsDataURL(file);
  } else {
    reader.onload = function (ev) {
      var pre = document.createElement('pre');
      pre.className = 'preview-text';
      pre.textContent = ev.target.result;
      preview.appendChild(pre);
    };
    reader.readAsText(file);
  }
});
</script>
"""

AVATAR_HTML = """
<div class="avatar-stage">
  <div id="avatar" class="avatar avatar-idle">
    <div class="avatar-glow"></div>
    <div class="avatar-thinking-dots"><span></span><span></span><span></span></div>
    <div class="avatar-head">
      <div class="avatar-eye avatar-eye-left"><div class="avatar-eye-shine"></div></div>
      <div class="avatar-eye avatar-eye-right"><div class="avatar-eye-shine"></div></div>
      <div class="avatar-blush avatar-blush-left"></div>
      <div class="avatar-blush avatar-blush-right"></div>
      <div class="avatar-mouth"></div>
    </div>
  </div>
</div>
"""

DROPZONE_HTML = """
<div id="drop-zone" class="drop-zone">Drag &amp; drop an image, video, or text file here</div>
<div id="preview-container" class="preview-container"></div>
"""

ui.add_head_html(HEAD_HTML, shared=True)


# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────

@ui.page("/")
def main_page():
    client = ui.context.client
    default_workspace = next((w for w in WORKSPACES if w.name == "SovereignOS"), WORKSPACES[0])
    state = {
        "persona": DEFAULT_PERSONA,
        "workspace_id": default_workspace.id,
        "history": load_chat_history(default_workspace.id),
        "auto_speak": False,
    }
    current_file = {"path": None, "kind": "text"}
    recording_state = {"active": False, "stop": False}
    speech_state = {"stop_event": None, "cache": {}, "last": None}

    # ---- FIX 1: thread-safety helpers -------------------------------------
    # Blocking work (mic recording, transcription, TTS playback) always runs
    # via run.io_bound(), which keeps execution on the SAME coroutine/task, so
    # NiceGUI's slot/client context is never lost across the await. On top of
    # that, every UI-facing update below goes through the captured `client`
    # object directly (client.run_javascript / `with client:`) rather than the
    # ambient ui.notify()/ui.run_javascript() shortcuts, so it can never hit
    # "slot stack empty" even if something one day calls it from an odd spot.
    def set_status(avatar_state: str, status_text: str):
        with client:
            client.run_javascript(
                f"window.setAvatarState('{avatar_state}'); window.setStatus('{_js_str(status_text)}');"
            )

    def toast(message: str, kind: str = "info"):
        with client:
            client.run_javascript(f"window.showToast('{_js_str(message)}', '{kind}')")

    def revert_to_idle():
        set_status("idle", "🟢 Online")

    # ---- Header --------------------------------------------------------
    with ui.row().classes("app-header w-full"):
        ui.html('<span class="app-title">SOVEREIGN <span class="app-title-accent">OS</span></span>')
        ui.html('<span id="status-indicator" class="status-indicator">🟢 Online</span>')
        initial_model = MODEL_ROUTER.candidates[0].info.label if MODEL_ROUTER.candidates else "No provider configured"
        model_indicator = ui.label(f"Ready: {initial_model}").classes("files-empty")
        ui.space()
        ui.switch("Speak replies", value=False,
                  on_change=lambda e: state.update(auto_speak=e.value)
                  ).props("color=amber")
        replay_speech_button = ui.button("Replay", icon="replay", on_click=lambda: replay_last_speech()).props("outline color=primary")
        replay_speech_button.disable()
        stop_speech_button = ui.button("Stop speech", icon="stop", on_click=lambda: stop_speech()).props("outline color=negative")
        stop_speech_button.disable()
        ui.button(icon="delete_sweep", on_click=lambda: clear_chat()).props("flat").tooltip("Clear chat history")
        ui.button("Memory", icon="psychology", on_click=lambda: open_memory_panel()).props("outline color=primary")
        workspace_select = ui.select(
            WORKSPACE_SERVICE.select_options(),
            value=state["workspace_id"],
            on_change=lambda e: switch_workspace(e.value),
        ).classes("workspace-select").props("dark outlined dense")
        ui.select(list(PERSONAS.keys()), value=state["persona"],
                  on_change=lambda e: state.update(persona=e.value)
                  ).classes("persona-select").props("dark outlined dense")

    # ---- Body ------------------------------------------------------------
    with ui.row().classes("app-body w-full no-wrap"):
        # LEFT: chat
        with ui.column().classes("chat-panel glass-panel"):
            chat_log = ui.column().classes("chat-log w-full")

            def add_bubble(role: str, text: str):
                bubble_id = f"sovereign-{role}-{time.time_ns()}"
                with chat_log:  # explicit slot per NiceGUI's own recommended pattern
                    ui.label(text).classes(f"chat-bubble chat-bubble-{role}").props(f"id={bubble_id}")
                return bubble_id

            def switch_workspace(workspace_id: str):
                try:
                    selected = WORKSPACE_SERVICE.get_workspace(workspace_id)
                    state["workspace_id"] = selected.id
                    state["history"] = load_chat_history(selected.id)
                    chat_log.clear()
                    for msg in state["history"]:
                        add_bubble("user" if isinstance(msg, HumanMessage) else "assistant", msg.content)
                    toast(f"Switched to {selected.name}. Memory and chat history are isolated.", "success")
                    refresh_memory_panel()
                except Exception as exc:
                    toast(f"Workspace switch failed: {exc}", "error")

            for _msg in state["history"]:
                add_bubble("user" if isinstance(_msg, HumanMessage) else "assistant", _msg.content)

            with ui.row().classes("chat-input-row w-full"):
                message_input = ui.input(placeholder="Message Sovereign...").classes("chat-input") \
                    .props("outlined dense dark")
                mic_button = ui.button(icon="mic").classes("icon-button")
                send_button = ui.button(icon="send").classes("icon-button")

        # RIGHT: avatar + files + drop zone
        with ui.column().classes("side-panel"):
            with ui.element("div").classes("glass-panel"):
                ui.html(AVATAR_HTML)
            with ui.column().classes("glass-panel files-panel w-full"):
                ui.label("Recent Files").classes("panel-title")
                files_column = ui.column().classes("files-list w-full")
            with ui.element("div").classes("glass-panel"):
                ui.html(DROPZONE_HTML)

    # ---- File modal (glass-morphism) --------------------------------------
    with ui.dialog() as file_dialog, ui.card().classes("glass-modal"):
        modal_title = ui.label("").classes("modal-title")
        modal_content = ui.textarea().classes("modal-textarea").props("outlined dark")
        modal_image = ui.image("").classes("modal-media")
        modal_video = ui.video("").classes("modal-media")
        modal_unavailable = ui.label("").classes("files-empty")
        with ui.row().classes("modal-actions w-full"):
            refine_button = ui.button("Refine with AI", icon="auto_awesome")
            format_select = ui.select(["txt", "md", "docx", "pdf"], value="txt").props("dense dark outlined") \
                .classes("modal-format-select")
            ui.button("Download", icon="download", on_click=lambda: on_download())
            ui.button("Close", on_click=file_dialog.close).props("flat")

    # Workspace memory manager: every operation uses the selected workspace scope.
    with ui.dialog() as memory_dialog, ui.card().classes("glass-modal"):
        ui.label("Sovereign Memory Vault").classes("modal-title")
        memory_scope_label = ui.label(f"Workspace: {default_workspace.name}")
        ui.label("Saving is explicit. Verify, export or permanently erase this workspace's records.")
        memory_panel_column = ui.column().classes("w-full")
        with ui.row().classes("modal-actions w-full"):
            ui.button("Export JSON", icon="download", on_click=lambda: export_memories())
            ui.button("Refresh", icon="refresh", on_click=lambda: refresh_memory_panel())
            ui.button("Close", on_click=memory_dialog.close).props("flat")

    with ui.dialog() as premium_dialog, ui.card().classes("glass-modal"):
        ui.label("Free model capacity unavailable").classes("modal-title")
        ui.label("Sovereign tried the configured free/local models. No paid model was called.")
        ui.label("Premium access is not yet activated for this installation.")
        if os.getenv("SOVEREIGN_PREMIUM_URL", "").strip():
            ui.link("View Sovereign premium options", os.getenv("SOVEREIGN_PREMIUM_URL", "").strip())
        ui.button("Close", on_click=premium_dialog.close).props("flat")

    def _open_media_lightbox(kind: str):
        url = current_file.get("url")
        if url:
            client.run_javascript(f"window.openLightbox('{kind}', '{_js_str(url)}')")

    modal_image.on("dblclick", lambda: _open_media_lightbox("image"))
    modal_video.on("dblclick", lambda: _open_media_lightbox("video"))

    def open_file_modal(path: Path):
        current_file["path"] = path
        current_file["url"] = f"/workspace-files/{path.name}"
        modal_title.set_text(path.name)
        ext = path.suffix.lower()

        modal_content.set_visibility(False)
        modal_image.set_visibility(False)
        modal_video.set_visibility(False)
        modal_unavailable.set_visibility(False)
        refine_button.set_visibility(True)
        format_select.set_visibility(True)

        if ext in IMAGE_EXTENSIONS:
            current_file["kind"] = "image"
            modal_image.set_source(current_file["url"])
            modal_image.set_visibility(True)
            refine_button.set_visibility(False)  # nothing textual to refine
            format_select.set_visibility(False)  # binary file — downloads unchanged
        elif ext in VIDEO_EXTENSIONS:
            current_file["kind"] = "video"
            modal_video.set_source(current_file["url"])
            modal_video.set_visibility(True)
            refine_button.set_visibility(False)
            format_select.set_visibility(False)
        elif ext in TEXT_EXTENSIONS or ext in DOCX_EXTENSIONS or ext in PDF_EXTENSIONS:
            current_file["kind"] = "text"
            try:
                modal_content.set_value(extract_text(path))
                modal_content.enable()
            except Exception as exc:  # noqa: BLE001
                modal_content.set_value(f"Could not read file: {exc}")
                modal_content.disable()
            modal_content.set_visibility(True)
            default_fmt = ext.lstrip(".")
            format_select.set_value(default_fmt if default_fmt in ("txt", "md", "docx", "pdf") else "txt")
        else:
            current_file["kind"] = "other"
            modal_unavailable.set_text("Preview isn't available for this file type — you can still download it.")
            modal_unavailable.set_visibility(True)
            refine_button.set_visibility(False)
            format_select.set_visibility(False)
        file_dialog.open()

    def refresh_files_list():
        files_column.clear()
        with files_column:
            files = list_recent_files()
            if not files:
                ui.label("No files yet — drop something in the workspace folder.").classes("files-empty")
            for f in files:
                with ui.row().classes("file-row w-full").on("click", lambda f=f: open_file_modal(f)):
                    ui.icon("description").classes("file-icon")
                    ui.label(f.name).classes("file-name")

    def refresh_memory_panel():
        memory_panel_column.clear()
        with memory_panel_column:
            selected_workspace = WORKSPACE_SERVICE.get_workspace(state["workspace_id"])
            memory_scope_label.set_text(f"Workspace: {selected_workspace.name}")
            records = WORKSPACE_SERVICE.list_memories(state["workspace_id"])
            if not records:
                ui.label("No saved memories yet. Ask Sovereign to remember something explicitly.")
            for item in records:
                with ui.column().classes("w-full glass-panel").style("padding: 10px; margin: 6px 0;"):
                    ui.label(item.content).classes("text-weight-medium")
                    ui.label(
                        f"{item.kind} | source: {item.source} | confidence: {item.confidence:.2f} | "
                        f"{'verified' if item.verified else 'unverified'} | revision {item.revision}"
                    ).classes("files-empty")
                    with ui.row().classes("w-full"):
                        if not item.verified:
                            ui.button(
                                "Mark verified", icon="verified",
                                on_click=lambda item_id=item.id: verify_saved_memory(item_id)
                            ).props("dense outline color=positive")
                        ui.button(
                            "Delete permanently", icon="delete",
                            on_click=lambda item_id=item.id: delete_saved_memory(item_id)
                        ).props("dense outline color=negative")

    def open_memory_panel():
        refresh_memory_panel()
        memory_dialog.open()

    def verify_saved_memory(memory_id: str):
        try:
            WORKSPACE_SERVICE.verify(state["workspace_id"], memory_id)
            refresh_memory_panel()
            toast("Memory marked as verified.", "success")
        except Exception as exc:
            toast(f"Could not verify memory: {exc}", "error")

    def delete_saved_memory(memory_id: str):
        try:
            if WORKSPACE_SERVICE.forget(state["workspace_id"], memory_id):
                toast("Memory and revision history permanently deleted.", "success")
            else:
                toast("Memory no longer exists.", "warning")
            refresh_memory_panel()
        except Exception as exc:
            toast(f"Could not delete memory: {exc}", "error")

    def export_memories():
        try:
            payload = WORKSPACE_SERVICE.export_json(state["workspace_id"])
            fd, path = tempfile.mkstemp(prefix="sovereign-memory-", suffix=".json")
            os.close(fd)
            Path(path).write_text(payload, encoding="utf-8")
            ui.download(path, filename="sovereign-memory-export.json")
            toast("Memory export prepared.", "success")
        except Exception as exc:
            toast(f"Memory export failed: {exc}", "error")

    async def on_refine():
        path = current_file["path"]
        if not path:
            toast("Nothing to refine.", "warning")
            return
        content = modal_content.value or ""
        set_status("thinking", "🟡 Processing")
        try:
            prompt = f"Refine and improve the following content. Return only the improved version:\n\n{content}"
            refined, model_info = await run.io_bound(
                call_llm, state["persona"], [], prompt, state["workspace_id"]
            )
            model_indicator.set_text(f"Running: {model_info.label}")
            modal_content.set_value(refined)
            toast("Refined with AI.", "success")
        except Exception as exc:  # noqa: BLE001
            toast(f"Refine failed: {exc}", "error")
        finally:
            revert_to_idle()

    def on_download():
        path = current_file["path"]
        if not path:
            return
        try:
            kind = current_file.get("kind", "text")
            if kind == "text":
                fmt = format_select.value or "txt"
                out_path = Path(tempfile.gettempdir()) / f"{path.stem}.{fmt}"
                write_text_as_format(modal_content.value or "", out_path)
                ui.download(str(out_path), filename=out_path.name)
            else:
                ui.download(str(path), filename=path.name)  # image/video/other — original file, unchanged
        except Exception as exc:  # noqa: BLE001
            toast(f"Download failed: {exc}", "error")

    refine_button.on_click(on_refine)
    refresh_files_list()

    # ---- Chat --------------------------------------------------------------
    def clear_chat():
        state["history"] = []
        chat_log.clear()
        save_chat_history([], state["workspace_id"])
        toast("Chat history cleared.", "info")

    async def stop_speech():
        """Request immediate cancellation of synthesis or playback."""
        event = speech_state.get("stop_event")
        if event is not None:
            event.set()
            client.run_javascript("window.finishSpeechSync();")
            set_status("idle", "⏹ Speech stopped")

    async def speak_safe(text: str, bubble_id: str):
        path = None
        stop_event = threading.Event()
        speech_state["stop_event"] = stop_event
        stop_speech_button.enable()
        replay_speech_button.disable()
        try:
            cached = speech_state["cache"].get(bubble_id)
            if cached is None:
                set_status("thinking", "🟡 Preparing voice — Stop is available")
                path, boundaries = await synthesize_speech(text, stop_event=stop_event)
                if stop_event.is_set():
                    return
                segment = await run.io_bound(load_audio_file, path)
                if stop_event.is_set():
                    return
                cached = {"path": path, "boundaries": boundaries, "segment": segment, "text": text}
                speech_state["cache"][bubble_id] = cached
                speech_state["last"] = bubble_id
                replay_speech_button.enable()

                # Keep only the five most recent generated responses in memory/disk.
                while len(speech_state["cache"]) > 5:
                    oldest_id = next(iter(speech_state["cache"]))
                    old = speech_state["cache"].pop(oldest_id)
                    if speech_state["last"] == oldest_id:
                        speech_state["last"] = bubble_id
                    await run.io_bound(remove_audio_file, old["path"])
            else:
                path = cached["path"]
                boundaries = cached["boundaries"]
                segment = cached["segment"]
                speech_state["last"] = bubble_id

            if stop_event.is_set():
                return
            client.run_javascript(
                "window.startSpeechSync(" + json.dumps(boundaries) + ", " + json.dumps(bubble_id) + ");"
            )
            await run.io_bound(play_audio_segment, segment, stop_event)
            if not stop_event.is_set():
                set_status("happy", "🟢 Online")
                ui.timer(1.2, revert_to_idle, once=True)
        except Exception as exc:  # noqa: BLE001
            if not stop_event.is_set():
                toast(f"Voice playback unavailable: {exc}", "warning")
            revert_to_idle()
        finally:
            stop_event.set()
            client.run_javascript("window.finishSpeechSync();")
            speech_state["stop_event"] = None
            stop_speech_button.disable()
            # Keep successfully generated audio after Stop/finish so Replay is instant.
            if path and bubble_id not in speech_state["cache"]:
                await run.io_bound(remove_audio_file, path)
            if speech_state.get("last") in speech_state["cache"]:
                replay_speech_button.enable()
            else:
                replay_speech_button.disable()

    async def replay_last_speech():
        bubble_id = speech_state.get("last")
        cached = speech_state["cache"].get(bubble_id) if bubble_id else None
        if not cached:
            toast("No generated speech is available to replay yet.", "info")
            return
        await speak_safe(cached["text"], bubble_id)

    async def send_message(text: str = None):
        text = text if text is not None else message_input.value
        if not text or not text.strip():
            return
        message_input.set_value("")
        add_bubble("user", text)
        set_status("thinking", "🟡 Processing")
        try:
            reply, model_info = await run.io_bound(
                call_llm, state["persona"], state["history"], text, state["workspace_id"]
            )
            model_indicator.set_text(f"Running: {model_info.label}")
            state["history"].append(HumanMessage(content=text))
            state["history"].append(AIMessage(content=reply))
            state["history"][:] = state["history"][-10:]
            save_chat_history(state["history"], state["workspace_id"])
            assistant_bubble_id = add_bubble("assistant", reply)
            refresh_files_list()  # picks up anything save_file/convert_file just wrote
            revert_to_idle()
            if state["auto_speak"]:
                await speak_safe(reply, assistant_bubble_id)
        except ModelRoutingError as exc:
            model_indicator.set_text("Model: unavailable")
            summary = str(exc).split("\nProvider details:")[0]
            add_bubble("assistant", summary)
            premium_dialog.open()
            toast("All configured free/local models are unavailable. No paid request was made.", "warning")
            revert_to_idle()
        except Exception as exc:  # noqa: BLE001
            toast(f"Something went wrong: {exc}", "error")
            revert_to_idle()

    async def on_mic_click():
        # Tap once to start recording, tap the same button again to stop early.
        # The transcript lands in the input box for you to review/edit — nothing
        # from voice is auto-sent anymore.
        if recording_state["active"]:
            recording_state["stop"] = True
            return
        recording_state["active"] = True
        recording_state["stop"] = False
        mic_button.props("icon=stop")
        set_status("listening", "🔵 Listening — tap the mic again to stop")
        try:
            raw_audio = await run.io_bound(record_until_stopped, recording_state)
            set_status("thinking", "🟡 Transcribing")
            text = await run.io_bound(transcribe_raw_audio, raw_audio)
            message_input.set_value(text)
            revert_to_idle()
        except Exception as exc:  # noqa: BLE001
            toast(str(exc), "error")
            revert_to_idle()
        finally:
            recording_state["active"] = False
            mic_button.props("icon=mic")

    send_button.on_click(lambda: send_message())
    message_input.on("keydown.enter", lambda: send_message())
    mic_button.on_click(on_mic_click)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ in {"__main__", "__mp_main__"}:
    if not MODEL_ROUTER.candidates:
        print("WARNING: No model configured. Add GROQ_API_KEY, OPENROUTER_API_KEY, or configure OLLAMA_MODEL.")
    ui.run(
        title="Sovereign OS",
        native=False,          # FIX 6: real browser window -> minimize/maximize/close all present
        reload=False,
        port=8080,
        dark=True,
        show=True,
    )
