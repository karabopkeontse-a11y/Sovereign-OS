# SovereignOS next ten objectives

1. One authenticated REST API contract for desktop, browser and Android.
2. Workspace identity and scoped conversation access, including cross-workspace rejection.
3. SQLite-backed API conversations in workspace scope.
4. Shared model health sourced from the central ModelRouter.
5. Responsive web command centre for workspaces, chat, models, IoT, approvals and settings.
6. Installable PWA with offline app-shell caching.
7. Capacitor Android wrapper and CI debug-APK artifact pipeline.
8. IoT device registry and telemetry interface, explicitly simulated until a gateway is connected.
9. Physical actuation blocked until trusted device identity, approval and audit infrastructure exist.
10. Continuous Python/web validation, API documentation and deployment handoff.

## Not yet complete
- Public hosting and domain: hosting scope currently needs reauthorization; no deployment is claimed.
- Multi-user/OAuth identity and tenant authorization: current REST API uses one operator bearer token.
- Real IoT sensors, MQTT gateway, firmware and actuator integration.
- Signed production APK and Play Store release: signing credentials must remain user-controlled.
- Apple client (deferred).
- Billing and premium entitlements.

## Run API
Install optional dependencies with `py -3.11 -m pip install -r requirements-api.txt`.
Set a long random `SOVEREIGN_API_TOKEN` in a private environment file, then run
`py -3.11 -m uvicorn sovereign_core.api:app --host 127.0.0.1 --port 8765`.
Set `SOVEREIGN_API_CORS_ORIGINS` to the exact HTTPS frontend origin before remote browser use.
Do not expose the API directly to the public internet. Use TLS and a trusted reverse proxy.

## Android
The Android workflow creates a debug APK and uploads it as a GitHub Actions artifact.
Only report a downloadable APK after the workflow succeeds. Production signing and Play Store
distribution require a release keystore controlled by the owner.
