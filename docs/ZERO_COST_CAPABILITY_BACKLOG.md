# SovereignOS Zero-Cost Capability Backlog

Status: prioritized implementation plan. Deliver the highest-value capability using existing hardware, open-source software, and genuinely free access before considering any paid dependency.

## Product rule

SovereignOS is project-agnostic. It starts with a General inbox. Users create projects from the UI when they choose. No business, client, assistant persona, or industry workflow is preloaded or mandatory. Optional templates may be offered later, but users must opt into them.

"Zero cost" means no required purchase, subscription, usage credit, or paid hosting to use the core capability. A free-tier API is an optional adapter, not a guaranteed always-free foundation. State any quota, account, network, or hardware requirement.

## P0 — Reliability and trustworthy foundations

1. **Speech controls**
   - Stop during synthesis and playback.
   - Retain recent generated audio for replay without re-synthesis.
   - Keep a bounded cache and explicit cleanup.
   - Keep avatar and word highlighting synchronized to playback.
   - For remote web clients, render audio in the browser/device; never play through the server's speakers.
   - Acceptance: stop and replay repeatedly; the next chat turn still works; no orphaned playback or unbounded cache.

2. **Project-agnostic registry**
   - General inbox works without a project.
   - Create, rename, archive, and select projects through UI.
   - Isolate notes, files, goals, and memory by project.
   - Acceptance: a clean installation contains no named business project.

3. **Problem-solving lifecycle**
   - Intake → clarify → diagnose → plan → authorize → execute → verify → report → learn.
   - Keep suggestions distinct from executed actions.
   - Approval required for external, irreversible, financial, publishing, deletion, or high-impact operations.
   - Completion requires explicit verification evidence.
   - Support cancellation, retry, blocked states, and resumable records.

4. **Persistence and recovery**
   - SQLite for local task and audit data; safe filesystem paths for artifacts.
   - Backups, restore validation, and migration/version handling.
   - Never log API keys, passwords, or unnecessary personal data.
   - Acceptance: restore a backup into a clean profile and verify record counts and sample contents.

5. **Engineering quality gate**
   - Compile/type-check, unit tests, integration tests, and production build in CI.
   - Do not report a feature as working based only on a successful commit.
   - Keep reproducible setup instructions and known limitations.

## P1 — Useful capabilities with free/open-source foundations

1. **Model gateway:** provider-neutral interface; selectable cloud API or local model; secrets remain local; show provider, model, limits, and possible costs. Prefer local inference (for example, Ollama or llama.cpp) when hardware supports it.
2. **Evidence-based research:** search adapters with source URL, retrieval time, and extracted evidence; label facts, estimates, assumptions, and unknowns; fail visibly when sources are inaccessible. Free endpoints may change or impose limits.
3. **Knowledge and files:** import text, Markdown, PDF, and office documents where free libraries support extraction; search by content and metadata; retain provenance; inspect originals. Add local embeddings only after basic indexed search is dependable.
4. **Tool registry:** typed inputs/outputs, permissions, timeouts, cancellation, logs, and verification hooks. Start with local filesystem, Python utilities, Git/GitHub, and explicitly connected accounts. External integrations remain disconnected until configured.
5. **Personal automation:** local schedules and repeatable checklists first; visible trigger, cadence, action scope, and pause/disable control; approvals for external side effects unless a narrow policy is explicitly configured.
6. **Opportunity review:** periodically review user-entered goals, blocked tasks, unused assets, and open opportunities; rank by expected value, feasibility, time, risk, and cost. Never invent demand, revenue, leads, or metrics.

## P2 — One codebase, three delivery formats

1. **Web app:** responsive desktop/mobile UI, local-first data, export/restore, installable PWA/offline shell where supported reliably, browser-side audio/mic/device capabilities.
2. **Windows desktop:** reuse the web UI inside Tauri; local Python engine as a managed sidecar/service; restrict filesystem access to approved paths; reproducible free build pipeline.
3. **Android APK:** reuse the web UI with Capacitor; explicit minimal native permissions; reproducible APK/AAB build; verify installation on a real device. Do not claim an APK exists until it is built and installed successfully.

## P3 — Higher autonomy, only after foundations pass

- Multi-step agent runner with checkpoints and resumable execution.
- Independent verification agents or deterministic test harnesses.
- Feedback memory recording what worked, failed, and why.
- Sandboxed code execution and allow-listed browser/computer actions.
- Resource monitoring, model fallback, budget caps, and automatic pause on quota/cost risk.

## Delivery sequence

1. Finish and locally verify the current speech replay change.
2. Make the Python core and UI project-agnostic; test create/select/archive project workflows.
3. Connect the problem-solving engine to chat through a narrow local API.
4. Complete a working responsive web client and test it end-to-end.
5. Package Windows desktop from the same client.
6. Package Android APK from the same client.
7. Add research, tools, and automation adapters one at a time, with tests and approval policies.

## Definition of a verified zero-cost feature

A feature qualifies only when it works without a required paid service or purchase, its dependencies and limits are documented, it has a repeatable test, it fails safely and visibly, and the user can disable it and export their data.
