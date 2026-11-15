# SovereignOS Adaptive Runtime Architecture

Status: architecture specification for implementation. This document defines the portable runtime; it does not claim that the features below are already implemented.

## 1. Product contract

SovereignOS is a hardware-adaptive, project-agnostic problem-solving engine. It must install on Windows, Android, Linux and suitable edge/IoT devices. Projects such as SmartFlow, NCS or any other user workspace are optional and created by the user.

The engine is not synonymous with its language model. The model is a replaceable inference provider. Deterministic tools, local data, task orchestration, permissions, verification, scheduling and recovery must remain useful when no model is available, offline, or out of API quota.

Core principles:
- Local-first and offline-capable. No mandatory cloud account, paid API, telemetry, subscription or hosted backend.
- Scan before installation and before model activation. Explain hardware findings and offer a recommended profile; never silently install optional software.
- User chooses optional runtimes, tools, model packs, API providers, permissions and storage locations.
- No hidden background processes. Default execution is on-demand. Any resident service, scheduled job, auto-start entry, network listener or device daemon requires explicit opt-in and a visible control to stop/remove it.
- Never silently incur costs or switch to a paid provider. Show provider, model, pricing/limits where known, estimated request cost where available, and require approval before paid calls.
- No secrets in source, model packs, logs, crash reports, task history, exported backups or Git.
- Evidence and verification are required before reporting a task as completed.

## 2. Layered architecture

1. Sovereign Core (portable domain layer)
   - Problem lifecycle, projects/workspaces, permissions, approvals, task queue, audit trail, verification, recovery and export/import.
   - No direct dependency on a specific model, UI, cloud provider or business vertical.
2. Adaptive Runtime
   - Hardware and platform profiler.
   - Capability policy and resource budget.
   - Model/provider router.
   - Tool/plugin registry and installer.
   - Lifecycle manager for loading, unloading, cancellation and cleanup.
3. Intelligence providers
   - Deterministic/no-model mode.
   - Local LLM runtime (initial target: llama.cpp with GGUF on supported desktop/Android platforms).
   - Optional platform-specific runtime adapters (e.g. ExecuTorch where model export and operator support are verified).
   - Optional user-configured cloud APIs.
4. Persistent knowledge
   - SQLite metadata and task history; files in user-controlled storage.
   - Search/indexing and provenance; optional local embeddings only where hardware permits.
   - Memory is separate from model weights and can be backed up, inspected, corrected, exported and deleted by the user.
5. Interfaces
   - Shared API/domain contracts; responsive web/PWA; Windows desktop shell; Android app; headless edge agent for supported Linux devices.
   - Do not make the browser UI the owner of privileged device actions.
6. Device/IoT integration
   - Explicit device adapters with narrow typed commands, authentication, allow-listed operations, timeouts and telemetry provenance.
   - Constrained microcontrollers use deterministic logic, small classifiers and sensor rules; heavier inference runs on a phone, gateway or server when available.

## 3. Hardware scan and adaptive profiles

Run a read-only scan before installation. Record platform/architecture, OS version, CPU model/features/core count, total and available RAM, GPU/NPU and supported backends, storage free space, battery/power state where available, thermal state where available, network state and permissions. Distinguish unknown from absent; do not treat shared graphics memory as dedicated VRAM.

The scan produces a signed-off recommendation report, not an automatic installation:
- **Minimal:** deterministic engine, SQLite, small utilities; no local LLM by default.
- **Constrained:** tiny quantized model only if measured RAM headroom, storage and latency test pass.
- **Standard:** small/medium quantized model with context and concurrency caps.
- **Accelerated:** GPU/NPU backend only after runtime capability detection and a smoke benchmark.
- **Gateway/server:** larger models and multi-device orchestration where memory, cooling and power are adequate.

The user's current Windows PC (Celeron N3060, 2 cores, 4 GB RAM, Intel HD Graphics, about 68.4 GB free) should start in Minimal mode. Do not promise useful local LLM inference on this device. A tiny model experiment may be offered only as an opt-in benchmark with a warning; do not install it by default. Keep the core useful without a model.

Resource manager requirements:
- Reserve RAM for OS/UI and enforce model memory budgets.
- Bound context length, batch size, concurrent tasks and cache size.
- Load model on demand and unload after configurable idle time; default unload at end of task/session.
- Cancel generation and release memory promptly.
- Respect battery saver, thermal throttling and metered networks on mobile.
- Provide a one-click low-resource mode and safe fallback to deterministic tools.
- Never swap or download a large model silently.

## 4. Model pack and persistent offline intelligence

A model pack is a separately versioned, integrity-checked artifact containing model weights, tokenizer/configuration, runtime compatibility metadata, license and attribution, source URL, SHA-256 hashes, supported architectures, expected memory/storage, context limits and test prompts/results.

Packaging rules:
- The base installer contains the engine and optionally a very small, explicitly licensed bootstrap model only if size and licensing permit.
- Larger model packs are optional downloads or user-supplied files; support resumable download, hash verification, disk-space preflight and atomic install.
- Never bundle a model unless its license explicitly permits redistribution and the exact terms/attribution are included. A model being open-weight does not automatically mean unrestricted redistribution.
- Keep model weights immutable during normal operation. Do not claim that memory edits or retrieval make the model weights themselves evolve.
- “Persistent and evolving offline intelligence” means a stable base model plus user-controlled memory, indexed knowledge, task outcomes, corrections, reusable procedures and optional versioned adapters/LoRA trained offline on capable hardware.
- Learning must be inspectable, reversible, attributable to a source and scoped to a project or General workspace. Do not train on secrets or external content without permission.
- Preserve previous model and memory versions for rollback. Model upgrades require user approval and regression tests.

## 5. Provider and API access

Define a provider-neutral interface with capabilities, limits, pricing metadata, authentication method, privacy/data-retention notes and availability status.

Provider selection:
- Offline/local is the default when an appropriate local model exists.
- If local inference is unavailable, use deterministic core capabilities and explain the limitation.
- Cloud providers are opt-in. User supplies and owns their API account and billing relationship.
- Store credentials using OS-native secure storage: Windows Credential Manager/DPAPI; Android Keystore; Linux Secret Service or an encrypted user vault. Never put tokens in .env shipped with installers, source, command-line arguments, UI state persistence, logs or crash dumps.
- Redact credentials from exceptions and diagnostics. Never send API keys to SovereignOS servers.
- Before a paid call, show provider/model, user-configured budget cap, estimated charge if provider supplies pricing, and ask for approval. If quota is exhausted or billing is unavailable, pause and ask the user. Never silently change providers, create a subscription or charge the developer.
- Provide per-provider disable, revoke and delete controls. API calls must be visible in an audit log without secret values.

## 6. Optional software and tool installation

Provide a Software & Tools catalogue. Every item declares:
- name, purpose, supported OS/architecture, license, source, version, size, dependencies, required permissions, network behavior, background behavior, cost/quota and uninstall procedure.
- install, update, run, stop and uninstall actions.
- sandbox/allow-list policy and risk classification.

The user can select individual tools. No tool is installed merely because the hardware scan recommends it. Downloads must use trusted official release sources, verify signatures/hashes when available, and show permissions before installation. Optional integrations remain disconnected until the user authenticates them.

## 7. Windows, Android and IoT deployment

### Windows
- Installer performs read-only preflight, shows profile and selected components, then installs only approved items.
- Use a user-writable data directory, secure credential store, SQLite migrations and clean uninstall.
- No Windows service, scheduled task, startup entry or firewall rule by default.
- Model runtime is launched on demand and stopped cleanly.

### Android
- Native app package contains UI, core client and compatible native runtime; model weights are a separate optional asset or user-selected download because model sizes vary.
- Use app-private storage and Android Keystore; request microphone, camera, files, network or device permissions only when a selected feature needs them.
- Use llama.cpp Android/NDK or a verified ExecuTorch backend based on model/runtime compatibility and device benchmarks.
- Support low-memory devices by disabling local LLM and retaining deterministic/offline features.
- Avoid unrestricted background execution; use Android-approved foreground services only for user-visible tasks that genuinely require them.

### IoT
- Do not put a general-purpose LLM on every microcontroller.
- Microcontroller tier: sensor acquisition, deterministic rules, tiny classifiers/anomaly detection, local safety interlocks and signed commands.
- Edge gateway tier: Linux-capable gateway runs the SovereignOS device agent, local database, optional compact model and authenticated device orchestration.
- Phone/desktop tier: richer planning, model interaction and management.
- SmartFlow/Thothi integrates as an optional application/workspace and device adapter, not as a hard-coded SovereignOS dependency.
- Critical safety decisions (e.g. water shutoff, electrical control) require deterministic bounds, fail-safe behavior and human-configurable policies; an LLM must not be the sole safety controller.

## 8. Privacy, security and safety

- Least privilege, explicit approvals for external effects, financial actions, publishing, deletion, device control and high-impact changes.
- Signed/verified updates, dependency pinning, software bill of materials and vulnerability review.
- Separate user data, credentials, model artifacts and application binaries.
- Encrypt sensitive local records where feasible; clearly document platform limitations.
- Local audit log records action, actor, permission, timestamp, result and evidence, never secrets.
- Network egress is deny-by-default for optional tools until enabled; show what data leaves the device.
- Provide export, backup, restore, delete, revoke and uninstall workflows.
- Device commands are authenticated, replay-resistant and constrained to explicit capabilities.

## 9. Verification and release gates

Do not call this architecture implemented until tests pass. Required gates:
1. Hardware profiler unit tests with fixtures for low-memory, no-GPU, shared-GPU, unknown hardware, Android and Linux.
2. Installer dry-run proves no optional component is installed and no background service/startup item is created without consent.
3. Model pack tests: license metadata present, checksum mismatch rejected, insufficient disk space rejected, interrupted download resumes, failed activation rolls back.
4. Resource tests: memory cap, context cap, cancellation, unload and repeated load/unload without unbounded growth.
5. Secret tests: scan logs, database, export, crash output and repository fixtures to confirm credentials are absent.
6. API budget tests: no call without consent, no call after budget/quota stop, no silent provider fallback.
7. Offline tests: airplane-mode operation of deterministic core, memory, projects, task lifecycle and local tools.
8. Android install and real-device smoke tests on at least one low-memory and one capable device.
9. IoT protocol tests with simulated devices, authentication failure, stale/replayed command rejection and safe fallback.
10. End-to-end acceptance: install -> scan -> choose profile -> select tools/model/provider -> run task -> verify -> stop -> restart offline -> recover memory -> uninstall cleanly.

## 10. Implementation sequence

P0: add platform-neutral capability/profile schema and read-only Windows profiler; write fixture tests; display scan report.
P1: introduce provider interface and deterministic no-model mode; add local model adapter behind feature flag.
P2: implement model-pack manifest, download/verify/install/uninstall and resource manager.
P3: implement secure credential abstraction and explicit provider/budget approval.
P4: implement optional software catalogue and permission/consent UI.
P5: make shared client responsive and package Windows desktop.
P6: Android native runtime and APK with model assets optional.
P7: edge gateway and IoT device protocol; integrate SmartFlow only as an optional application.
P8: offline memory/knowledge learning, versioned adapters and regression/rollback tools.

## 11. Definition of done

A device has been adaptively installed only when the preflight report is saved, the user approved the exact selected components, all selected components passed smoke tests, no unapproved background activity exists, secrets are protected, offline behavior is demonstrated, and uninstall/rollback is tested.
