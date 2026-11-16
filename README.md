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
