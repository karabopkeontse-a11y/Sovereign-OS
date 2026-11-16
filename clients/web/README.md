# SovereignOS Web / PWA / Android

This is a responsive PWA and Capacitor-ready Android shell for the Python core. No model API keys are bundled in the frontend.

## Run
```bash
npm install
npm run dev
npm test
npm run build
```

## API connection
Install optional Python dependencies with `py -3.11 -m pip install -r requirements-api.txt`. Set a long random `SOVEREIGN_API_TOKEN` privately, then run `py -3.11 -m uvicorn sovereign_core.api:app --host 127.0.0.1 --port 8765`. Set `SOVEREIGN_API_CORS_ORIGINS` to the exact HTTPS frontend origin before remote use. Use TLS and a trusted reverse proxy. Do not expose the API directly to the public internet.

Enter API URL and bearer token in Settings. The token remains in page memory only; reload clears it.

## PWA
Serve the built `dist` directory over HTTPS (localhost is accepted by Chromium). Android Chrome can install it from the browser menu. Offline mode caches only the app shell.

## Android
```bash
npm install
npm run build
npx cap add android
npx cap sync android
cd android
./gradlew assembleDebug
```
The debug APK is created at `android/app/build/outputs/apk/debug/app-debug.apk`. A successful GitHub Actions artifact is required before claiming an APK exists. Production signing and Play Store release need a user-controlled keystore.

## IoT safety
Device records are explicitly simulated. The API rejects physical commands with HTTP 403. Real hardware needs an authenticated gateway, device identity, signed messages, replay protection, allow-listed commands, human approval and audit logs.
