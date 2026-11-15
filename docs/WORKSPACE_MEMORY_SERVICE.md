# Workspace Memory Service

## What changed

SovereignOS now has a provider-neutral `WorkspaceService` in
`sovereign_core.workspaces`. It is the shared application boundary for desktop,
web and Android clients. It composes the existing SQLite task and memory stores;
it does not introduce a cloud dependency or a new database format.

The service can provision these initial workspace identities idempotently:

- General
- Nikolai Creative Studio
- Yard Truths
- SmartFlow BW
- SovereignOS

Clients select a workspace and pass its ID to this service. The service validates
that the workspace exists and checks memory ownership before correction, history
access or deletion. This prevents a client from using a memory ID from another
workspace through the service API.

## Minimal use

```python
from sovereign_core.workspaces import WorkspaceService

service = WorkspaceService("sovereign_workspace/sovereign_tasks.sqlite3")
workspaces = service.ensure_default_workspaces()
selected = next(w for w in workspaces if w.name == "SmartFlow BW")

service.remember(
    selected.id,
    "Thothi is the customer-facing SmartFlow assistant powered by SovereignOS.",
    kind="architecture_decision",
    source="user-confirmed project decision",
    confidence=1.0,
    verified=True,
)
memories = service.search(selected.id, "Thothi SovereignOS")
```

## Client and security contract

- The client/session owns the active workspace selection.
- Never let the language model choose or override the active workspace ID.
- A desktop client should keep selection in its per-client UI state.
- A web/mobile adapter must derive the workspace ID from an authenticated,
  authorized session, not from an arbitrary request body.
- The SQLite workspace ID is an application-level boundary, not authentication.
  Do not expose one local database to untrusted users.
- Memory content is not automatically ingested from chat. Explicit-save policy
  remains the responsibility of the calling client.
- No cloud sync, encryption-at-rest, account identity, automatic backup or
  conflict-resolution protocol is added here.

## Verification

```bash
python -m unittest discover -s tests -v
```

The new tests cover idempotent workspace provisioning, case-insensitive creation,
memory isolation, correction history, cross-workspace ID rejection and unknown
workspace rejection.
