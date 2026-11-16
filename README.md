# SovereignOS

SovereignOS is the shared intelligence and execution core for separate workspaces and client surfaces. Thothi is the SmartFlow BW-facing assistant powered by this core; NCS and Yard Truths retain separate workspace context.

## Existing desktop application

Run the Windows desktop/NiceGUI client with Python 3.11 and the project's existing dependencies:

```bat
py -3.11 sovereign_os_ultimate.py
```

## New REST API and web client

See [API and platform milestone](docs/NEXT_TEN_OBJECTIVES.md) and [web/PWA/Android guide](clients/web/README.md).

Install API dependencies:

```bat
py -3.11 -m pip install -r requirements-api.txt
```

Set a private random bearer token in your local environment file, then start the API:

```bat
py -3.11 -m uvicorn sovereign_core.api:app --host 127.0.0.1 --port 8765
```

The API refuses authenticated routes until `SOVEREIGN_API_TOKEN` is configured. CORS is disabled unless `SOVEREIGN_API_CORS_ORIGINS` is explicitly set. Do not expose it directly to the public internet.

## Core principles

- Free/open-source-first and no automatic paid model calls.
- Workspaces isolate conversations and memories.
- Provider credentials stay on the backend.
- External, financial, destructive and physical-device actions require explicit approval.
- IoT sample telemetry is labelled simulation; physical commands are blocked until a trusted gateway exists.
- Do not claim a web deployment or APK until its deployment/build workflow has succeeded.


## Model provider pool

The API router supports Groq, OpenRouter, Cloudflare Workers AI, Z.AI, SambaNova, NVIDIA NIM, local Ollama, and Mistral (explicit paid opt-in). Credentials remain in the API host environment.

OpenRouter can expose multiple model candidates through one existing `OPENROUTER_API_KEY`. The default fallback pool includes:
- `nvidia/nemotron-3.5-super-vl:free` for multimodal reasoning (text, images and video)
- `nvidia/nemotron-3-ultra-550b-a55b:free` for long-context reasoning
- `cohere/north-mini-code:free` for coding

These are third-party free endpoints and can be rate-limited. Do not send confidential or sensitive workspace content to free endpoints unless their data terms are acceptable to you. Set `OPENROUTER_FALLBACK_MODELS` to a comma-separated list of model IDs to customise the pool. A single OpenRouter key is reused; no new paid provider is enabled by this setting.


## Visual conversations and voice

The web Intelligence composer accepts up to four PNG, JPEG or WebP images (4 MB each). Image understanding requires manually selecting the OpenRouter Free vision candidate `nvidia/nemotron-3.5-super-vl:free` in Model health. Image data is sent to that provider for the current request and is not stored as an image in the conversation database; the text history records that an image was attached. Do not send sensitive images to a third-party free endpoint.

Call your AI remains a separate browser voice session with live recognition status, a call transcript, speech output and interruption. The current implementation uses browser speech recognition and speech synthesis with short voice-mode responses. It is not yet a PSTN telephone call or a low-latency full-duplex audio stream. Calling a deployed SovereignOS from a phone will require a secure public backend plus WebRTC or a telephony provider, authentication, session routing and abuse controls.
