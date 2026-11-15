# Nikolai-V1 → SovereignOS migration record

**Source:** [karabopkeontse-a11y/Nikolai-V1](https://github.com/karabopkeontse-a11y/Nikolai-V1)  
**Destination:** [karabopkeontse-a11y/Sovereign-OS](https://github.com/karabopkeontse-a11y/Sovereign-OS)  
**Migration branch:** `migration/nikolai-core-to-sovereign`

## Migration rule

SovereignOS is the generic engine and system of record. Nikolai is an application/brand and opportunity-intelligence service that can integrate with SovereignOS through a permissioned adapter. Do not merge the two repositories wholesale.

## Ported in this change

- Deterministic specialist-role classification from Nikolai's `packages/core/src/agentRouter.ts`.
- Human-approval classification principles from `packages/core/src/autonomy.ts`.
- Reimplemented in Python using SovereignOS's existing `PermissionLevel` model.
- Added offline unit tests for routing, permission classification, and empty input handling.

The port is intentionally classification-only. It does not call a model, invoke tools, perform an action, or bypass `ProblemEngine` approval states.

## Keep in Nikolai-V1

These are application-specific or deployment-specific and should remain in the Nikolai repository:

- `nikolai_backend/`: public opportunity discovery API and its Render deployment.
- `nikolai_web/`: Nikolai-branded opportunity-hunting interface.
- `android/`: Nikolai Android wrapper and its app identity.
- `social_lead_hunter/`: lead-hunting application and its UI/workflows.
- Nikolai-specific web assets, marketing, investor demo and campaign materials.
- Render deployment configuration and app-specific GitHub Actions.

SovereignOS may later consume the opportunity service as an optional, user-enabled connector. Do not copy its server, deployment credentials, or application UI into the core.

## Do not port as runtime code

- `venus.json`, `hustle.json`, `exo35.json`: these are named application/persona presets, not generic offline models. Preserve them in Nikolai until the user explicitly chooses to install them as optional profiles.
- `fallbackLogic.ts`: its retry-and-fallback helper is too small to become Sovereign's provider router; use the adaptive runtime/provider interfaces in `docs/ADAPTIVE_RUNTIME_ARCHITECTURE.md`.
- `contextTrim.ts`: truncates input to 512 characters and can silently discard important context.
- `deepthink.ts`, `streamOut.ts`, `culturalUX.ts`: inspect and redesign against Sovereign's provider-neutral core before porting; do not import the old TypeScript runtime into the Python core.
- `commerce.ts`: commerce/order domain belongs in an optional commerce integration, not the generic engine.

## Architecture alignment

- Existing SovereignOS SQLite store already contains workspace-scoped `memory_records`; this is a local foundation, not cloud sync or an authenticated multi-device vault.
- Existing `ProblemEngine` and `PermissionLevel` remain authoritative for task lifecycle and execution gates.
- The recovered adaptive runtime architecture remains the target for hardware profiling, model packs, provider selection, API cost approval, Android packaging and IoT safety boundaries.
- SmartFlow/Thothi is an optional application and IoT adapter. It must not become a hard-coded core dependency.

## Acceptance gates

1. Run `python -m compileall -q sovereign_core tests`.
2. Run `python -m unittest discover -s tests -v`.
3. Review the diff before merging into `main`.
4. Do not claim online memory, cross-device sync, local model inference, or Android/IoT deployment from this migration. Those require separate implementations and tests.
