# SovereignOS 2070 — Product North Star and Cumulative Roadmap

**Status:** Governing product direction  
**Repository:** karabopkeontse-a11y/Sovereign-OS  
**Active development line:** feature/durable-collaboration-recovery

## 1. Product definition

SovereignOS is a personal, fully agentic operating system and execution environment. It is not merely a chatbot, a collection of agent demos, or a dashboard of disconnected features.

It should accept a goal, understand constraints, plan work, delegate to specialist agents, use explicitly permitted local resources and online services, execute workflows for hours or overnight, recover from interruptions, verify outcomes, preserve evidence, and deliver useful reports and completed artifacts.

The product target is **at least 200 distinct, meaningful, working capabilities** delivered through one unified experience: native capabilities, plugins, application integrations, automation packs, and workspace tools. This is a firm product target, not an aspiration or a number to reduce. The user is comfortable with a desktop installation of up to approximately 4 GB when justified by useful capabilities. Do not inflate the footprint with redundant dependencies or placeholder features.

## 2. Product outcomes

1. **Autonomous execution:** missions run for hours or overnight, with durable state, scheduling, retries, provider failover, checkpoints, recovery, and clear status.
2. **Online and offline operation:** local models and local tools continue without connectivity; online models, web research, and cloud integrations activate when reachable. The UI clearly explains capability and connectivity state.
3. **Authorized personal environment:** access user-approved files, folders, applications, devices, and connected accounts through explicit scopes and revocable permissions.
4. **Web intelligence and action:** research, compare, monitor, create, publish, and operate permitted web workflows with evidence and approval-aware controls.
5. **Practical income workflows:** discover and validate opportunities, prepare deliverables, support approved commercial workflows, track real outcomes, and report progress. Never fabricate leads, revenue, or task completion; consequential financial or external actions require approval.
6. **Agent Collaboration:** persistent Agent Council, Leader coordination, specialist proposals, peer review, direct responses, task ownership, evidence, verification, and private Leader briefings.
7. **Morning Intelligence:** completed work, artifacts, discoveries, opportunities, verified outcomes, pending approvals, failures, usage where available, and next actions.
8. **Exceptional UX:** polished command centre, live mission visibility, clear workspace context, useful defaults, rich application surfaces, accessible controls, and consistent visual language.
9. **Extensible ecosystem:** at least 200 distinct, meaningful, working capabilities across plugins, plugin styles, applications, integrations, and automation packs, discoverable and manageable through one coherent catalog.
10. **Multi-device product:** shared web experience first, then Windows desktop, then Android, without fragmenting the core engine.

## 3. Non-negotiable architecture principles

- Preserve the existing Python application and evolve it incrementally; do not replace the foundation for novelty.
- Workspaces remain independent: conversations, memory, files, identities, agent tasks, discussions, plugin configuration and permissions, and UI state. Shared model/tool infrastructure must not leak workspace data.
- Preserve the existing plugin registry, workspace metadata, workspace-plugin configuration and permissions, and all working voice functionality.
- All long-running work has durable identity, status, checkpoints, evidence, audit history, bounded retries, and restart recovery.
- Browser/client disconnects must not cancel backend work. Reconnect and status polling must reattach to the same run.
- Use idempotency to prevent duplicate missions, discussions, messages, purchases, posts, and other external effects.
- Human approval gates apply to consequential actions: spending, payments, purchases, publishing, sending messages, changing permissions, deleting or overwriting important data, and other irreversible actions.
- Use least privilege, explicit scopes, credential isolation, secret redaction, and auditable tool calls.
- Prefer free/open/local capabilities by default. Never silently switch to paid models or paid services; paid providers require explicit opt-in.
- No fabricated search results, leads, metrics, citations, earnings, or success claims. Verify outputs and communicate uncertainty.
- Use unittest, not pytest. Make small, testable changes and preserve a cumulative decision log/backlog.
- Never stage unrelated files or use destructive Git cleanup/reset/pull shortcuts.

## 4. Capability portfolio — minimum target: 200 verified capabilities

Organize capabilities into coherent families rather than a flat list:

1. Agent runtime, planning, delegation, memory, collaboration, evaluation, and governance.
2. Mission scheduler, queues, triggers, recurring workflows, long-running execution, and morning reporting.
3. File intelligence: permissioned indexing, search, reading, extraction, transformation, creation, versioning, and safe recovery.
4. Web intelligence: search, browsing, source verification, monitoring, comparison, extraction, and permitted web actions.
5. Local computer and application control with explicit permissions, confirmations, and action receipts.
6. Communication: voice, transcription, speech, email, calendar, notifications, and multi-agent live calls.
7. Business and income workflows: opportunity discovery, research, content production, lead qualification, proposals, bookkeeping support, storefront operations, and outcome tracking.
8. Creative studio: documents, spreadsheets, presentations, images, audio, video, design, and publishing workflows.
9. Developer and IT operations: code, tests, Git, deployments, diagnostics, logs, databases, and controlled automation.
10. Personal productivity: tasks, projects, notes, schedules, knowledge, routines, and briefings.
11. Integrations and plugin ecosystem: connectors, local plugins, remote plugins, permissions, lifecycle, health, versioning, and marketplace/catalog.
12. System experience: command centre, workspace switcher, activity timeline, mission monitor, app launcher, settings, accessibility, theming, onboarding, and device sync.

Every catalog item must have a clear user outcome, permission model, online/offline classification, dependencies, verification strategy, and tests. Maintain a cumulative feature register with stable IDs and lifecycle states: PLANNED, IN DEVELOPMENT, TESTING, VERIFIED, RELEASED. Count a capability toward the 200 only after its implementation and acceptance tests pass. Do not count trivial UI toggles, duplicate variants, disconnected placeholders, or merely displayed controls as features.

## 5. Delivery roadmap and order

### Milestone A — Verify and harden current Agent Collaboration recovery
- Sync the current feature branch safely and verify its exact head.
- Run focused unittest suites and syntax checks locally.
- Fix integration failures before expanding scope.
- Validate idempotent mission creation, atomic worker claims, checkpoint persistence, resume after provider failure, workspace isolation, and client reconnect.
- Complete bounded automatic retry with exponential backoff, jitter, Retry-After handling, provider-specific error classification, and explicit manual retry fallback.
- Verify process-restart recovery and ensure concurrent API workers cannot duplicate a run.
- Keep a cumulative decision log and feature backlog.

### Milestone B — Sovereign Mission Runtime

**Phase 1 — Persistent mission queue (implementation committed; focused local acceptance tests passed 2026-11-23):**
- Schema v6 adds workspace-scoped mission jobs and append-only mission event history without replacing existing plugin/workspace/collaboration tables.
- Mission creation supports idempotency keys, JSON payloads, scheduled timestamps, priority and bounded attempt budgets.
- Atomic worker claims prevent duplicate concurrent execution; worker leases and heartbeats identify ownership.
- Expired leases can be reclaimed after a process/worker interruption; exhausted jobs transition to failed rather than remaining stuck as running.
- Guarded lifecycle transitions cover pause, resume, retryable, completion, failure and cancellation.
- Persistence acceptance tests cover migration preservation, idempotency, workspace isolation, due-time gating, concurrent claims, restart recovery, exhausted leases, heartbeat ownership and lifecycle audit events.
- Local acceptance evidence reported by the user on 2026-11-23: collaboration recovery 9/9 and mission runtime 10/10 passed. Orchestration 7/7 had passed at the previous checkpoint. Phase 1 is validated against these focused suites; this is not a claim that the full repository suite passed.

**Phase 2 — Scheduler and execution supervision (in progress):**
- Durable one-time schedules implemented and locally acceptance-tested (16/16 mission runtime tests passed 2026-11-23): timezone-aware timestamps, auditable lifecycle events, cancellation, and atomic idempotent promotion into the existing mission queue.
- Recurring daily/weekly schedule definitions and occurrence promotion are implemented in this slice; focused local acceptance is pending. DST gap policies (`skip` / `shift_forward`) and ambiguous-time policies (`first` / `second`) are explicit, and each occurrence promotes idempotently into the existing mission queue.
- Next: event-triggered and user-triggered scheduling.
- Separate execution from web request lifetime.
- Per-stage deadlines, heartbeat/lease supervision, cancellation, pause/resume, resource limits, and graceful shutdown.
- Persisted plans, tool calls, outputs, artifacts, evidence, approvals, and audit events.
- Morning report generator and notification delivery.
- Local-first execution and explicit online-provider fallback policies.

### Milestone C — Permissioned Personal Computer and File Access
- User-selected folders and file scopes, allow/deny rules, read/write separation, and revocation.
- File search/indexing and content extraction with workspace boundaries.
- Controlled application/computer-use adapters with visible action logs and confirmation gates.
- Safe file writes, versioning, recovery, and protection against accidental destructive actions.

### Milestone D — Online Intelligence and Approved Action
- Reliable web research, source capture, verification, and monitoring.
- Connector/plugin execution with per-workspace credentials and scopes.
- Approval workflow for publishing, sending, purchasing, payments, and other external effects.
- Business opportunity workflows that distinguish discovery from verified opportunity and verified income.

### Milestone E — Agent Council and Voice
- Persistent council and roster, task assignment, live discussion, evidence review, and private Leader reports.
- Multi-agent voice participation with distinct identities, Leader-controlled turn taking, and no overlapping speech.
- Interruptible voice, replay, transcripts, and consistent workspace context.

### Milestone F — 200+ Verified-Capability Ecosystem and Jaw-Dropping UX
- Coherent app launcher, capability catalog, plugin manager, automation builder, and mission command centre.
- Deliver and maintain the minimum 200-capability feature register; report verified/released counts separately from planned and in-development counts.
- Plugin styles: native/local, connected account, remote service, and user-defined workflow, all under one lifecycle and permission model.
- Live mission timeline, progress visualization, evidence drawer, approvals inbox, morning briefing, and operational health.
- Responsive shared web app, followed by Windows desktop and Android.
- Build capabilities in tested vertical slices; do not create 200 disconnected placeholders.

### Milestone G — Reliability, Distribution, and Growth
- Offline-first packaging, local model management, updates, backups, observability, diagnostics, and recovery.
- Windows desktop release and Android application after shared UX and API contracts stabilize.
- Resource-aware overnight operation, with user-configurable CPU/GPU/network/budget limits.
- Security review, permission review, recovery drills, and end-to-end tests.

## 6. Current engineering checkpoint

The immediate engineering thread is **durable Agent Collaboration recovery** on branch feature/durable-collaboration-recovery.

Previously confirmed local tests before the latest API/UI batch:
- Recovery store tests: 5 passed.
- Orchestration tests: 9 passed.
- sovereign_core/orchestration.py compiled successfully.
- Earlier full-suite baseline: 100 tests passed. This is a historical baseline, not a claim about the current branch.

The user confirmed commit 17223a3 was pushed successfully. Subsequent GitHub edits added or adjusted:
- Schema v5 request-key idempotency and atomic discussion/run creation.
- Atomic worker claim and worker scheduling.
- Durable orchestration checkpoint resume and assignment/discussion idempotency.
- API queue/status/retry handling and interrupted-run recovery.
- Frontend polling, reconnect restoration, and same-key submission replay.
- Tests for durable resume, atomic worker claims, API idempotency, and workspace isolation.

**Verification status (2026-11-23):** the user confirmed the latest local focused suites passed: collaboration recovery 9/9, orchestration 7/7, and API 33/33, for 49/49 tests. The user also invoked `node --check clients\\web\\src\\main.js`; no syntax error was included in the supplied output. This is focused validation, not a full-suite pass or completion of Milestone A. Continue with provider error classification, bounded retry/backoff, restart/lease recovery, and concurrency tests. Do not overwrite unrelated local files.

## 7. Immediate next actions

1. Confirm the current remote branch head and changed file list.
2. User safely fetches and fast-forwards only if the local branch is the expected feature branch and has no conflicting tracked changes.
3. Run focused unittest suites: collaboration recovery, orchestration, API; run Python compile checks and node --check.
4. Fix failures one at a time and rerun affected tests.
5. Finish automatic retry/backoff and restart/lease recovery.
6. Phase 1 is validated by focused local tests. Proceed to Phase 2 with a narrow, test-first scheduler/execution-supervisor slice.

## 8. Product acceptance standard

A feature is not complete merely because it appears in the UI. It must:
- solve a real user problem;
- work across the correct workspace boundary;
- declare required permissions and whether it works offline;
- persist and recover state when appropriate;
- expose understandable progress and errors;
- verify outputs and preserve evidence;
- include unittest coverage and integration checks;
- have an intentional, polished UX;
- avoid silent paid usage and unapproved consequential actions.

**North-star test:** the user can assign SovereignOS a legitimate, well-scoped mission in the evening, allow it to work within configured permissions and resource limits, leave the computer unattended, and receive an accurate morning report with completed artifacts, evidence, exceptions, and next steps.
