# SovereignOS 2027: adaptive, local-first architecture

## Product direction

SovereignOS is one intelligence and execution core with clients for Windows, Linux, Android, browser/PWA and trusted IoT gateways. Workspaces such as General, SmartFlow BW (Thothi), Nikolai Creative Studio and Yard Truths are isolated contexts on the same platform, not competing assistants.

The product priorities are:
- Local-first operation and useful offline capability.
- Voice as a first-class interaction, alongside text and automation.
- Adaptive routing between local models and explicitly permitted cloud models.
- Small, modular base installation with optional capability packs.
- Transparent system state: every connection, model selection, permission, background task and failure is visible.
- Safe-by-default execution with approvals, audit history and reversible operations where possible.

## Installation strategy: up to 4 GB, not 4 GB required

A 4 GB upper budget is a ceiling for a fully provisioned optional installation, not a minimum download and not a promise that every device can run every included model.

| Tier | Contents | Target |
|---|---|---|
| Core | Shell, workspace manager, memory, API, permissions, updater and diagnostics | All supported devices |
| Voice pack | Speech capture, local or browser transcription adapters, speech output, wake/turn controls | Devices with microphone/audio support |
| Local intelligence pack | Quantized small language models, embeddings and local inference runtime | Hardware-qualified devices |
| Specialist packs | Coding, translation, vision, document extraction and domain tools | Opt-in, per-device |
| Cloud connectors | Provider adapters and cost/consent policy; no bundled secrets | Online devices |

The installer should measure available RAM, CPU architecture, disk space, GPU/NPU capability, battery and network conditions before recommending optional packs. Downloads are resumable and integrity-checked. Users can remove packs without damaging the core.

A 4 GB RAM machine must not automatically download or load a multi-gigabyte model. On such hardware, SovereignOS should use lightweight local services for memory, search, orchestration, rules and caching, then use an explicitly configured cloud model when the task justifies it. Local model inference remains optional and capability-gated.

## Adaptive intelligence and routing

The ModelRouter becomes a policy engine rather than only a list of API clients.

1. Classify the task and its sensitivity.
2. Check whether a local model or deterministic local tool can handle it.
3. Estimate context size, latency, memory pressure and likely cloud cost.
4. Prefer local execution when quality and hardware allow.
5. Use a free cloud tier only when configured and available.
6. Use a paid provider only after explicit user opt-in and within a user-defined budget.
7. Explain the route chosen and record provider, model, latency, usage/cost when the provider exposes it.
8. Never silently escalate to paid usage.

Local memory, retrieval, indexing, policy checks, task planning and cached knowledge should remain useful without a model connection. Cloud sync must be workspace-scoped, encrypted in transit, conflict-aware and configurable by data category. Secrets must never sync as ordinary memory.

## Call Your AI: voice session design

The first web implementation is a browser voice session:
- Browser speech recognition captures and transcribes the user's speech.
- The transcript is sent to the active workspace through the existing authenticated API.
- The configured SovereignOS model generates the response.
- Browser speech synthesis reads the answer aloud.
- The interface exposes listening, thinking, speaking, error and disconnected states.
- The user can end the session at any time.

This first version is not a telephone/PSTN call and browser speech services may use browser-vendor cloud infrastructure. A later native voice engine should support local speech-to-text and text-to-speech packs, voice activity detection, echo cancellation, interruption/barge-in, language selection and offline operation. Real phone calling would be a separate optional SIP/telephony integration with explicit consent, cost disclosure and jurisdiction-aware recording controls.

## Device and IoT architecture

Clients communicate with the same versioned API. Physical devices connect through a trusted gateway using device identities, signed messages, replay protection, least-privilege command scopes, human approval and tamper-evident audit events. The current API's IoT records are simulation-only; actuation stays blocked until this security layer is built and tested.

## Experience requirements

- Every tab changes state visibly and communicates loading, success, empty, offline and error states.
- Every action confirms whether it was accepted, queued, completed or rejected.
- Model cards show configured/readiness/cooldown states without exposing secrets.
- Approval requests show action, requester, target, scope, risk, expiry and evidence.
- Voice sessions show microphone permission, transcription, model processing and playback states.
- Reduced-motion and keyboard-accessible modes are mandatory.
- No fake metrics, simulated model responses or fabricated device states presented as live.

## Delivery sequence

### Phase A: current web foundation
- Improve visual hierarchy, interaction feedback, model health and approval states.
- Add browser-based Call Your AI with explicit limitations.
- Validate web build, API tests and workspace isolation.

### Phase B: voice and local runtime
- Define a stable speech adapter interface.
- Add optional local STT/TTS engines and a local inference adapter.
- Add hardware profiling and pack management.
- Add voice turn-taking, interruption and offline tests.

### Phase C: unified multi-device platform
- Windows and Linux desktop shell over the shared API/core.
- Android client with secure token storage and microphone permissions.
- PWA offline shell and install/update lifecycle.
- Trusted IoT gateway and device identity.

### Phase D: adaptive sync and distribution
- Encrypted workspace sync with user-controlled scope.
- Incremental, signed, rollback-capable updates.
- Capability-based installer with optional packs and integrity checks.
- Release signing, reproducible builds, accessibility and security audits.

## Release gates

Do not call the platform finished until each client is tested on real target hardware; offline, low-memory and network-loss paths are verified; local/cloud routing is explainable; paid use is opt-in; data isolation and deletion are tested; updates can roll back; and the Android release is signed by the owner. A passing web build is only one gate, not proof of a complete operating system.
