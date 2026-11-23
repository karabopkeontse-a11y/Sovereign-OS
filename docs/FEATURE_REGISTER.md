# SovereignOS Capability Register

**Product target:** at least 200 distinct, meaningful, working capabilities.
**Counting rule:** only capabilities in `VERIFIED` or `RELEASED` count, and only after their acceptance tests pass. Planned, implemented, and testing items do not count.
**Lifecycle:** `PLANNED → IN DEVELOPMENT → TESTING → VERIFIED → RELEASED`.

Each capability must have a real user outcome, permission model, online/offline classification, dependencies, verification strategy, and tests. Duplicate variants, decorative controls, and disconnected placeholders are excluded.

## Current count

| Measure | Count |
|---|---:|
| Verified / released | 19 |
| In testing | 0 |
| Planned | 0 |
| Minimum product target | 200 |

Mission queue, scheduler, and worker capabilities are counted only where their focused acceptance suites have passed; MISSION-012 has now completed its registered-agent and worker-recovery acceptance checks. The 200-capability target remains in progress.

## Mission runtime

| Stable ID | Capability | User outcome | Permissions | Connectivity | Dependencies | Verification / tests | Status |
|---|---|---|---|---|---|---|---|
| MISSION-001 | Durable mission creation | Save a mission so it survives closing the browser or restarting the app | Workspace-scoped mission-create permission | Offline-capable | SQLite mission store | Reopen store and retrieve same mission | VERIFIED |
| MISSION-002 | Mission idempotency | Repeated submissions do not create duplicate jobs | Same workspace and idempotency key | Offline-capable | MISSION-001 | Replay same key; reject changed input | VERIFIED |
| MISSION-003 | Workspace-isolated mission records | Users see and operate only missions in the active workspace | Workspace membership | Offline-capable | Workspace store | Cross-workspace retrieval returns no record | VERIFIED |
| MISSION-004 | Scheduled eligibility | Future missions are not claimed before their due time | Mission-create permission | Offline-capable | Durable timestamps | Future-dated job remains unclaimed | VERIFIED |
| MISSION-005 | Priority-aware queue ordering | Higher-priority eligible work is claimed first | Worker permission | Offline-capable | MISSION-001 | Competing eligible jobs claim in priority order | VERIFIED |
| MISSION-006 | Atomic worker claims | Concurrent workers cannot execute the same queued mission | Worker identity | Offline-capable | SQLite transactional claim | Concurrent claim test yields one winner | VERIFIED |
| MISSION-007 | Worker lease and heartbeat | A worker can prove it is still responsible for a running mission | Lease-owner identity | Offline-capable | MISSION-006 | Heartbeat extends lease; foreign worker is rejected | VERIFIED |
| MISSION-008 | Expired-lease recovery | Work can be reclaimed after worker or process interruption | Worker identity | Offline-capable | MISSION-007 | Restart store and reclaim expired lease | VERIFIED |
| MISSION-009 | Bounded mission attempts | Interrupted work cannot retry forever | Mission policy | Offline-capable | MISSION-008 | Exhausted expired lease becomes failed | VERIFIED |
| MISSION-010 | Audited mission lifecycle | Pause, resume, completion, failure and cancellation have durable event history | Workspace-scoped mission control; running changes require lease owner | Offline-capable | MISSION-001 | Verify valid transitions and ordered event history | VERIFIED |

| MISSION-011 | Independent durable mission worker supervisor | Execute claimed missions outside API and browser lifetimes and persist verified outcomes | Workspace-scoped worker identity; explicit executor registration; lease-owner-only lifecycle updates | Offline-capable core; executor declares its own connectivity | MISSION-001 through MISSION-010; explicit executor interface | Result persistence, heartbeat, retry backoff, non-retryable failure, attempt exhaustion, shutdown, workspace isolation; user-reported local acceptance: 10/10 passed (2026-11-23), including heartbeat lease-loss protection and process-restart reclaim | VERIFIED |

| MISSION-012 | Permission-aware registered agent step execution and recovery | Execute approved workspace tools once, persist execution intent and reconcile interrupted file operations without overwriting conflicting data | Workspace-scoped task/step; persisted approval for non-read-only actions; registered tools only | Offline-capable workspace tools | MISSION-011; TaskStore; registered workspace tools | Atomic concurrent claim; receipt persistence and replay; crash after note creation; effect-applied recovery; conflicting-file protection; worker adapter approval enforcement and shutdown replay (4/4); worker heartbeat lease-loss and process-restart reclaim (10/10); service suite (9/9) | VERIFIED |

MISSION-012 is VERIFIED from the user's reported focused local acceptance results on 2026-11-23: execution service 9/9, registered-agent worker integration 4/4, and worker supervisor/recovery 10/10. The lease-loss test confirms a stale worker cannot persist its result after losing heartbeat ownership; the restart test confirms a new worker reclaims an expired lease and completes the existing mission. This is not a full-repository test claim.\n\n## Mission scheduling

| Stable ID | Capability | User outcome | Permissions | Connectivity | Dependencies | Verification / tests | Status |
|---|---|---|---|---|---|---|---|
| SCHED-001 | Durable one-time schedule definitions | Save a future mission schedule that survives application restarts | Workspace-scoped mission scheduling permission | Offline-capable | SQLite schedule store | Migration, persistence, workspace isolation, idempotent create | VERIFIED |
| SCHED-002 | Timezone-aware one-time scheduling | Schedule a mission using an explicit IANA timezone and UTC offset | Workspace-scoped mission scheduling permission | Offline-capable | SCHED-001; Python zoneinfo and tzdata | Reject naive, unknown, or mismatched timezone inputs; normalize due time to UTC | VERIFIED |
| SCHED-003 | Idempotent due-schedule promotion | Convert each due schedule into exactly one existing queue job | Workspace-scoped scheduler/queue permission | Offline-capable | SCHED-001; mission queue | Due gating, atomic concurrent promotion, queue linkage, audit events | VERIFIED |

| SCHED-004 | Durable recurring schedule definitions | Save daily or selected-weekday missions that survive restarts | Workspace-scoped scheduling permission | Offline-capable | Schema v8; IANA timezone database | Daily/weekly persistence, idempotency, workspace isolation, restart tests | VERIFIED |
| SCHED-005 | Explicit daylight-saving-time policies | Choose how nonexistent and ambiguous local times are handled | Schedule-owner configuration | Offline-capable | SCHED-004; Python zoneinfo | Spring-forward skip/shift and fall-back first/second-fold tests | VERIFIED |
| SCHED-006 | Idempotent recurring occurrence promotion | Catch up due occurrences without duplicate queue jobs after restart or concurrent scheduler workers | Workspace-scoped scheduler/queue permission | Offline-capable | SCHED-004; mission queue | Missed-run catch-up, unique occurrence keys, concurrent promotion and audit tests | VERIFIED |

| SCHED-007 | Independent mission scheduler supervisor | Promote due one-time and recurring schedules without relying on API requests or browser sessions | Workspace-scoped scheduler process; read/promote only | Offline-capable | SCHED-001 through SCHED-006; SQLite store | Multi-workspace promotion, repeated-cycle idempotency, failure isolation, health reporting, bounded loop and graceful stop; user-reported local acceptance: supervisor 5/5 and mission runtime 23/23 passed (2026-11-23) | VERIFIED |

SCHED-007 is verified from the user's focused local acceptance results: scheduler supervisor 5/5 and mission runtime 23/23 passed (2026-11-23). This is not a full-repository test claim.

MISSION-011 is verified from the user's focused local acceptance result: worker supervisor 8/8 passed (2026-11-23), with worker/store syntax compilation also successful. The heartbeat test checks the persisted heartbeat during execution; terminal transitions correctly clear active heartbeat and lease fields. This is not a full-repository test claim.

## Backlog policy

- Add a stable ID before implementation; never recycle IDs.
- Record new capabilities as `PLANNED`, move them through the lifecycle, and preserve the test evidence for every transition.
- Update the count only when a capability is verified or released.
- Keep capability definitions outcome-based and distinct. A UI surface does not count separately from the underlying working capability unless it independently delivers a meaningful user outcome.
- Expand the portfolio across all 12 capability families in `SOVEREIGNOS_NORTH_STAR.md`; do not inflate the count by splitting one operation into cosmetic subfeatures.
