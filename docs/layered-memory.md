# Layered memory and retrieval

Agent4Good stores three memory layers in SQLite or PostgreSQL.
Existing shared notes remain available through the Memory page and named-note tools.
Memory records are private workspace data, stored as plaintext alongside task content.

## Layers and trust

| Layer | Written by | Visibility | Contents |
|---|---|---|---|
| Working | Runtime checkpoints or approved task writes | Exact task only | Current objective, pending tools, latest observation |
| Episodic | Runtime completion/failure or approved writes | Root runtime results are project-wide; child and explicit task records remain private | Past execution observations, with no independent success claim |
| Semantic | Owner or exact-approved task writes | Owner's project | Sourced facts, decisions, instructions, strategies, failures, repository knowledge, documentation, architecture, and tasks |

The server sets ownership, task identity, visibility, timestamps, actor, version, and trust classification.
Models cannot set these fields. All retrieved content remains `untrusted_data`, including owner-authored stored instructions.
Only the current owner request and server policy govern execution. Store standing action permissions through the policy API.
A retrieved sentence cannot authorize a tool, change credentials, or bypass approval.
The project remains single-owner. Database binding enforces tenant and environment isolation.
Private child snapshots and episodes never appear in parent or sibling retrieval.
Only the child's deliberately returned result reaches its parent through worker result collection.

Every record includes a source, confidence, creation timestamp, actor, and optional supersession ID.
Confidence describes a claim, not proof. Repetition never increases it automatically.
Runtime outcomes describe observed execution, not independent factual verification.

## Retrieval and budgets

Before each model step, the runtime searches with the original task prompt.
It filters by owner and task visibility before ranking candidates.
The search considers at most the newest 1,000 active, visible records.
It ranks normalized term overlap, adjusted for record length, with deterministic recency and ID ties.
Records with no matching meaningful terms are excluded.
It returns at most ten complete records, skipping records that exceed the remaining context budget.

`A4G_MEMORY_CONTEXT_BUDGET=4096` controls the retrieved context allowance. Set `0` to disable retrieval.
The supported range is 0 through 32,000. UTF-8 bytes conservatively bound model tokens.
The budget includes serialized provenance and the trust warning. It is not an exact tokenizer measurement.
The total provider-call reservation also includes retrieved context and model protocol overhead.
Retrieval context does not accumulate in saved conversation history. Events retain selected IDs and byte usage.

Automatic reads respect inherited `memory_search` permissions and policy call/cost limits.
Approval-only or denied automatic reads are omitted. Explicit searches use the normal audited tool path.
A child without `memory_search` cannot receive automatic retrieval.
Model routes pin from the original durable transcript before retrieved context is attached.

This is lexical retrieval over a semantic fact layer, not vector or embedding search.
It does not infer synonyms, crawl repositories, import documentation, or guarantee recall beyond the candidate cap.
Supply concrete source text through the owner API or exact-approved memory tools.

## Tools

- `memory_read`: read the current semantic record for a known key.
- `memory_write`: retain the existing key/content contract with exact approval and version history.
- `memory_search`: search with `{"query":"PostgreSQL queue leases"}` within the configured budget.
- `memory_store`: exact-approved structured writes. Its `record` field contains a JSON string.

Example decoded `record` value:

```json
{
  "layer": "semantic",
  "kind": "architecture",
  "key": "queue",
  "content": "The PostgreSQL queue uses fenced worker leases.",
  "source": "docs/distributed-infrastructure.md, durable queue",
  "confidence": 0.8,
  "supersedes": null
}
```

Allowed kinds are `repository`, `documentation`, `architecture`, `decision`, `task`, `strategy`, `failure`, `instruction`, and `fact`.
Keys use 1–64 letters, numbers, underscores, or hyphens. Content is limited to 20,000 characters.
Sources are required and limited to 500 characters. Confidence must be finite and between zero and one.
Credential checks and redaction apply to owner and tool writes.

## Inspect and correct

Use an authenticated owner session. Writes require its `X-CSRF-Token` and the configured origin.

| Endpoint | Purpose |
|---|---|
| `GET /api/memory-records?history=true&offset=0` | Inspect records, supersession history, and quarantine markers, 100 per page |
| `GET /api/memory-records?task_id=TASK_ID` | Inspect project records and that task's private records |
| `POST /api/memory-records` | Save the JSON record above as the owner |
| `POST /api/memory-records?task_id=TASK_ID` | Save working or episodic records attached to a specific task |
| `GET /api/tasks/TASK_ID/memory?query=PostgreSQL` | Preview the records that fit that task's retrieval budget |

To correct a record, copy its current ID into `supersedes` and submit the same layer and key.
Private corrections must remain within the original task. Project corrections may cite a new source.
A conflicting active key without `supersedes` returns HTTP 409.
Concurrent corrections allow one winner. A second correction must inspect the new version before proceeding.
The old record remains inspectable but leaves retrieval immediately.
The existing Memory page still edits named notes. New layer inspection uses the authenticated API above.

## Migration, corruption, and recovery

SQLite schema 7 and PostgreSQL schema 3 add memory records and migration markers.
Initialization imports each legacy note once without removing its original row.
Repeated startup does not duplicate imports. Invalid legacy content stays in the original table and is quarantined.
Normal named-note edits update both the compatibility row and versioned record in one transaction.
Corrections to a compatibility note through the new API also update its old row.

Readers validate the document schema, identity columns, version, timestamp, and SHA-256 digest.
Invalid records become quarantined and never enter retrieved context or named reads.
History inspection returns a safe marker for damaged content. It does not return malformed document bytes.
Correct a quarantined record explicitly by its ID, using a known good source or private backup.
The runtime rebuilds damaged working snapshots from durable execution records, never from corrupt memory.
Unknown versions are quarantined rather than guessed.

Hashes detect accidental corruption. They do not protect against an attacker who controls the database and can replace hashes.
Whole-database damage still requires the documented backup/restore procedure.
Memory does not replace execution checkpoints, receipt checks, or policy records.

SQLite backups include all layers. PostgreSQL backup format 3 includes memory and migration tables.
Restore accepts formats 1 and 2 and derives their missing memory layer from restored legacy notes.
Offline SQLite-to-PostgreSQL migration accepts schema 5, 6, or 7.
Version history is retained. There is no automatic retention purge in this release.

## Verification

`uv run pytest tests/test_memory.py -q` checks real SQLite storage and the authenticated API.
It covers all layers, migration, budgets, relevance exclusion, private workers, conflicting corrections, corruption, and restart recovery.
It also proves that a scripted model following hostile memory text still cannot execute an unapproved write.
These tests verify server enforcement, not a live model's resistance to every prompt injection.

`tests/test_infrastructure.py::test_layered_memory_runtime_backup_restore` runs against real PostgreSQL in CI.
It checks runtime storage, portable restoration, supersession, and corruption exclusion.
The model responses are scripted. Live model completion and production use remain unverified.
