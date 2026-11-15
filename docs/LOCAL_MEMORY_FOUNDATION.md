# SovereignOS Local Memory Foundation

## Purpose

The local memory store gives SovereignOS a deterministic, offline-first persistence layer for facts, preferences, decisions and validated procedures. It is a foundation for retrieval and future synchronization, not a hosted memory service or an autonomous learning model.

## Implementation

- Module: `sovereign_core.memory.MemoryStore`
- Storage: SQLite; pass the same database path as `TaskStore` to share the local database.
- Isolation: every operation is scoped by an explicit `workspace_id`. Authentication and multi-user ownership are intentionally not implied.
- Provenance: each record stores `kind`, `source`, confidence from 0 to 1, and a verified flag.
- Lifecycle: active, superseded and retracted records. Corrections create revisions; superseding creates a new record and retains the old record's history.
- Retrieval: deterministic case-insensitive keyword matching across all query terms, scoped to one workspace. Verified-only retrieval is supported.
- Portability: export a workspace as JSON, including records and revision history.
- Erasure: `delete(memory_id)` permanently removes the record and its revision history. A successor's link is cleared if its predecessor is erased.

## Minimal use

```python
from sovereign_core import MemoryStore

memory = MemoryStore("sovereign_workspace/sovereign_tasks.sqlite3")
record = memory.remember(
    workspace_id="my-project",
    content="Use a lightweight SQLite store on the local device.",
    kind="decision",
    source="user confirmation",
    confidence=1.0,
    verified=True,
)
results = memory.search("my-project", "SQLite local device", verified_only=True)
```

## Safety and current limits

- No cloud account, network request, API key, model provider or background process is required.
- The current workspace ID is an application-level isolation boundary, not a security principal. Do not expose a shared database to multiple untrusted users.
- Keyword retrieval is intentionally simple and predictable. Semantic/vector retrieval can be added behind the same API later, without making it mandatory.
- There is no cross-device sync, encryption-at-rest layer, authenticated owner identity, automatic backup or conflict-resolution protocol in this change.
- Memory writes are explicit API calls. The store does not silently infer or save facts from chat.
- Existing legacy `memory_records` data is not migrated or modified by this module. A separate reviewed migration is required before adopting any old records.

## Verification

Run the offline suite with:

```bash
python -m unittest discover -s tests -v
```


## Desktop chat integration

The desktop assistant uses the same SQLite file at `sovereign_workspace/sovereign_tasks.sqlite3` and a dedicated `general` memory scope.

- Relevant keyword-matched memories are retrieved locally and supplied to the assistant before a model call.
- The assistant can save a memory only after an explicit user request.
- Saved items begin unverified. The user can mark them verified in the Memory Vault.
- The Memory Vault lists active general memories, exports JSON, and permanently deletes a selected record and its revision history.
- Chat tools support explicit remember, search, list, correct/supersede, revision-history inspection and forget operations.
- Corrections create a new active record and retain the old record as superseded; they do not silently overwrite the old fact.

This first chat integration is intentionally limited to the general scope. Project-specific memory selection and authenticated multi-device synchronization are not yet implemented. The chat still uses the configured Groq model for responses; local memory retrieval itself does not consume model tokens.
