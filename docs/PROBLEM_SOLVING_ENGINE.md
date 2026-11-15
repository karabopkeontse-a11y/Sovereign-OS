# SovereignOS Problem-Solving Engine
Version: 0.1
Status: Build specification; implementation begins with the speech-control stabilization milestone.

## Product boundary

SovereignOS is the shared intelligence and execution platform. It owns reasoning orchestration, tool access, project memory, task planning, permissions, execution records, verification, and feedback.

It exposes separate workspaces:
- **Nikolai Creative Studio (NCS):** publishing, brand assets, SEO, content operations, marketing, lead discovery, customer workflows, and revenue experiments.
- **SmartFlow BW / Thothi:** water-leak intelligence, customer-facing assistance, alerts, pilot operations, product support, and property/water-system workflows.

Thothi remains the SmartFlow-facing assistant and product identity. It calls SovereignOS capabilities through a controlled service interface; it is not a second copy of the SovereignOS application.

## Core problem-solving cycle

Every substantial task follows this lifecycle:

1. **Intake** — capture the user's goal, workspace, constraints, deadline, and expected outcome.
2. **Clarify** — ask only for information that blocks safe or correct execution.
3. **Diagnose** — inspect available project context and evidence; separate facts, assumptions, and unknowns.
4. **Plan** — produce small, ordered actions with dependencies, risks, and a measurable definition of done.
5. **Authorize** — distinguish read-only work, reversible edits, external side effects, and high-impact actions. Ask approval where required.
6. **Execute** — use registered tools through narrow, auditable interfaces.
7. **Verify** — inspect the result independently; run tests or compare before/after state. Never equate a successful API response with a working outcome.
8. **Report** — state what changed, what was verified, what remains blocked, and the next action.
9. **Learn** — store durable decisions, outcomes, and failure patterns in the correct workspace.

A task is not marked complete until its verification criteria pass or the remaining limitation is explicitly recorded.

## Target architecture

```
NCS workspace ─────┐
                   ├── SovereignOS Core API
SmartFlow / Thothi ┘       |
                           +-- Project & Memory Registry
                           +-- Problem Intake / Task State Machine
                           +-- Planner and Policy Gate
                           +-- Tool Registry / Adapters
                           +-- Execution Queue and Audit Log
                           +-- Verification and Feedback
                           +-- Model Gateway (provider/model configurable)
                           +-- User Interface / Voice / Notifications
```

### Separation of concerns

- **Core:** provider-independent task lifecycle, policies, memory contracts, tool interfaces, and verification.
- **Adapters:** Groq/LLM, search, weather, files, GitHub, email, databases, social platforms, and future local tools.
- **Workspaces:** isolated project instructions, files, goals, credentials references, and memory namespaces.
- **Interfaces:** current NiceGUI desktop app first; SmartFlow/Thothi API integration later.
- **Storage:** local-first SQLite for task records and metadata; filesystem for workspace artifacts. Secrets remain in environment variables or a dedicated secret store and are never written to task logs.

## Initial data model

- `Workspace`: id, name, purpose, policy profile, memory namespace, enabled tools.
- `Problem`: id, workspace_id, title, request, constraints, status, priority, created_at.
- `PlanStep`: id, problem_id, order, action, dependencies, permission_level, status.
- `ToolRun`: id, step_id, tool_name, input_summary, output_summary, started_at, ended_at, outcome.
- `Verification`: id, step_id, criterion, method, result, evidence_reference.
- `MemoryRecord`: id, workspace_id, kind, content, source, confidence, created_at.

Statuses: `new`, `clarifying`, `diagnosing`, `planned`, `awaiting_approval`, `running`, `verifying`, `completed`, `blocked`, `failed`, `cancelled`.

## Safety and operating rules

- Default to free/open-source and low-cost options; disclose recurring costs before adopting a paid dependency.
- Never fabricate search results, weather, customer leads, metrics, test results, or completed actions.
- Read-only tools may run under workspace policy. External publishing, messaging, payments, deletion, and irreversible changes require explicit authorization.
- Keep NCS and SmartFlow customer data isolated by workspace.
- Use least-privilege tool permissions and allow-listed filesystem paths.
- Store evidence references and concise summaries, not secrets or unnecessary personal data.
- Every running task must support cancellation and a visible status.
- Model output is a proposal, not an instruction to bypass policy or tool permissions.

## Build sequence

### Milestone 0 — Stabilize the operator experience
- Add a visible Stop speech control.
- Make audio playback interruptible with low stop latency.
- Keep avatar and word highlighting tied to the playback clock.
- Reduce time-to-first-audio; avoid waiting for a long full-response synthesis.
- Ensure speech can be disabled without affecting text chat.
- Acceptance: stop during preparation and playback; no audio continues; avatar returns to idle; next chat turn works.

### Milestone 1 — Extract a reusable core
- Move configuration, model access, tool registration, task state, and persistence out of the monolithic UI file.
- Add SQLite task and execution records.
- Implement workspace registry and per-workspace memory namespaces.
- Keep the existing NiceGUI app as a client of the core rather than the owner of business logic.

### Milestone 2 — Build the problem-solving loop
- Implement intake, diagnosis, planning, authorization, execution, verification, reporting, and learning.
- Add cancellation, retry policy, timeouts, and resumable task states.
- Add structured evidence and completion criteria.

### Milestone 3 — Activate NCS workspace
- Load the NCS project brief, brand rules, Blogger/Facebook publishing workflow, content standards, and current blockers.
- Add content QA and publish-ready verification.
- Add lead/revenue experiments with measurable outcomes; no invented performance claims.

### Milestone 4 — Power SmartFlow / Thothi
- Define a stable internal API for Thothi to submit tasks and retrieve approved answers/actions.
- Connect water-leak knowledge, device/property context, alert workflows, and pilot feedback.
- Add strict tenant/customer isolation and human escalation for uncertain or high-impact cases.

### Milestone 5 — Reliability and autonomy
- Add scheduled jobs only after task execution and verification are dependable.
- Add local/offline model options where hardware permits.
- Add health checks, structured logs, cost tracking, backups, and recovery.

## Definition of done for the first usable engine

A user can submit a problem under NCS or SmartFlow; SovereignOS records it in the correct workspace, creates an evidence-based plan, requests approval for consequential actions, executes permitted steps, verifies results, and retains a resumable audit trail. A failed tool call is visible as a failure, not silently presented as success.
