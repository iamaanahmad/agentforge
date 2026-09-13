# Specialist workers

A parent task can delegate bounded work to any of the nine roles. Children run through the existing engine, registry, policy and model router. They are durable tasks, not additional role labels.

## Coordination contract

The model receives five new tools. Inputs use the registry's strict string-field schema.

| Tool | Contract |
|---|---|
| `worker_spawn` | `request` is a JSON object containing `title`, `prompt`, `agent`, `tools`, `priority`, and `reason`. Role IDs come from `/api/agents`. `tools` is an explicit subset of the parent's list. |
| `worker_message` | `recipient` is a direct parent or child task ID. `content` is at most 4,000 characters. Messages persist; siblings must communicate through their parent. |
| `worker_context` | `scope` is `private` or `shared`. `operation` is `read` or `write`. Reads use empty `revision` and `content`. Writes supply the expected revision as a string and up to 10,000 content characters. Stale writes fail without replacing newer context. |
| `worker_results` | No arguments. Returns caller identity, direct child statuses and results, and the last ten inbound messages. Results are capped at 750 characters per child; messages at 1,000. Full results remain in the owner API. |
| `worker_wait` | No arguments. Checkpoints and yields the execution slot until direct children finish, fail, or cancel. Then the parent resumes its saved transcript and can collect results. |

Delegation needs a concrete independent deliverable and a reason. The system prompt requires minimum permissions and tells parents to wait, inspect evidence, and disclose child failures. This instruction guides planning; the server enforces limits. It does not judge whether a delegation reason is wise.

Example `worker_spawn` request:

```json
{"title":"Review supplied evidence","prompt":"List contradictions in the supplied facts.","agent":"research_analyst","tools":["worker_context","worker_message","worker_results","artifact_write"],"priority":1,"reason":"Independent evidence review alongside implementation"}
```

A child starts with its explicit prompt, not the parent's transcript. Private context is accessible only through that task's own tool calls. Shared context belongs to the root tree. Neither context is project memory. Children can read project notes only when granted `memory_read`; they cannot receive `memory_write`. Context and messages are untrusted data, not permission grants. The owner can inspect all tasks through the authenticated API.

## Bounds and authority

| Setting | Default | Scope |
|---|---|---|
| `A4G_MAX_CONCURRENT_RUNS` | 4 | SQLite active runners or global PostgreSQL claims |
| `A4G_MAX_WORKER_TREE_SIZE` | 16 | Root plus all descendants, including finished tasks |
| `A4G_MAX_WORKER_DEPTH` | 2 | Maximum child depth; root is zero |
| `A4G_MAX_WORKER_TREE_STEPS` | 60 | Durable model attempts across the tree, including retries |
| Existing model token and estimated cost limits | Existing owner values | Aggregate reservations across the root tree |
| Existing daily run cap | 30 | All started run segments, including parent resumes |

Each worker keeps existing per-task step, retry, timeout and recovery bounds. Priorities range from -10 to 10. Higher priority claims first; ties use creation time and task ID. This is not preemptive scheduling and has no aging guarantee.

Children cannot enlarge tool lists. Current policy rules are evaluated for the child and every ancestor, including ancestor rate limits. A parent denial cannot be bypassed by selecting another role. Exact external-write approvals remain mandatory and are not inherited. Vault role access must also permit every ancestor. Existing provider routes still select by role and work type; there is no automatic fallback.

Shared-context writes compare revisions in one transaction. GitHub writes reserve the category for one active task, conservatively preventing concurrent repository edits. Memory writes reserve the note key. Successful closed tasks release reservations on the next write. Ambiguous receipts keep reservations for owner inspection, even after failure. Browser sessions and artifacts retain task ownership. This does not promise exactly-once external effects.

## Execution and recovery

SQLite keeps one daemon per volume, with a bounded thread pool. Each runner has its own engine, registry and provider context. PostgreSQL replicas use the same pool behind existing global admission, leases and fencing. Short database transactions serialize control changes; provider calls run outside transactions.

Spawn, message and context mutations commit atomically with their tool receipts. Replaying a completed spawn reuses the same child. Restart recovery preserves transcripts, messages and contexts. `waiting_children` parents consume no execution slot. The scheduler wakes them when direct children are terminal.

Cancelling a parent cancels unfinished descendants and rejects their pending approvals. A failed child cancels its descendants, while siblings continue. Existing model/read transport retry limits apply separately to each child. Failed external writes never automatically retry. A parent cannot finish while children remain active. Aggregation does not prove factual correctness. [Quality contracts](independent-quality.md) add separate critic and verifier workers with executable evidence checks.

An already-running external request may finish after cancellation. Threads share a trusted Python process and kernel; they are not OS security sandboxes. Generated code remains confined to the optional coding broker.

## Migration and evidence

SQLite schema 6 and PostgreSQL schema 2 add separate coordination tables without replacing owner tasks. Existing SQLite schema 5 can migrate through an offline backup. Portable backup format 2 includes task trees, contexts, messages and write reservations. Format 1 backups remain readable with empty coordination tables.

`tests/test_workers.py` exercises two real concurrent runner threads, separate contexts, transactional conflicts, parent permission denial, priorities, limits, retries, cancellation, and tree budgets. Its daemon acceptance starts the real worker process, waits for two children to run together, kills the process, restarts it, and checks collected results and restored private context. The fixture deletes its temporary data after completion.

`tests/test_infrastructure.py::test_distributed_child_workers_and_backup` exercises two real PostgreSQL runners and restores a populated tree into a separate schema. CI supplies PostgreSQL and object storage. Model responses in these checks are scripted. No live provider completion or public production operation is established by these tests.
