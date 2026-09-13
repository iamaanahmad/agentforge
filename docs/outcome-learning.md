# Outcome learning

Agent4Good reuses execution evidence during planning. It does not train models or change action permissions.
No comparative test establishes improved success rates, cost savings, or causal strategy effects.

## What gets recorded

Completed, failed, and cancelled tasks produce outcome records. Cancellation is an abandoned attempt, including unstarted drafts.
The original task history stays intact. Startup queues historical terminal tasks for backfill without inventing missing data.
Durable change markers survive crashes. Workers collect up to 100 markers before claims and after execution.
Authenticated outcome inspection also collects pending records. Large migrations may need several worker polls.
Late provider or tool receipts refresh accounting without creating another attempt.
Each distinct terminal timestamp identifies an attempt. Immutable hashed snapshots preserve earlier accounting versions.

Each outcome contains the objective, role, transcript hash, complete stored plan, actions, tool receipts, final result, and error.
Artifact IDs, task links, execution checks, quality rounds, and reviewer identities link the observation to source evidence.
These are the stored observations, not independent confirmation that every external action succeeded.
Duration is wall time from execution start through termination, including approval and review waits.
Unstarted and migrated tasks without start evidence have unknown duration.

Costs use each task's own model-call rows, provider-reported tokens, and pinned owner-configured prices.
Unknown usage or prices produce null estimated cost, never zero. Reservations remain separate from usage estimates.
Failed or uncertain calls retain their reservations and uncertainty. Late accounting updates preserve prior snapshots.
Estimates exclude tool charges and child calls. Sum distinct task records for a tree; do not count snapshots twice.
No value represents an invoice or a provider billing reconciliation.

## Trust and planning

Root outcomes are available to later tasks within the same owner's project. Child outcomes stay private.
Independent reviewers receive no outcome history. Worker permissions and `memory_search` policy govern automatic reads.
Tenant and environment database binding remains mandatory. This release still supports only one owner.
Outcome records and notes are private plaintext workspace data. API responses and model context use credential redaction.

The runtime ranks at most 100 recent visible outcomes by literal term overlap with the original objective.
It supplies at most five complete summaries. Half the configured memory byte budget is available for outcomes.
Unused bytes remain available for ordinary memory. Oversized and irrelevant entries are omitted.
Recall is lexical, bounded, and not guaranteed. It uses neither embeddings nor automatic web research.

Every entry is untrusted data and explicitly has `verified_fact: false`.
`configured_checks_passed` requires an accepted round with both critic and verifier passes on a completed task.
That label applies only to configured assertions. It never proves that an approach caused success.
A normal completed task remains `unverified_outcome`. Failed and abandoned attempts receive the same relevance ranking.
Owner correlations, interpretations, and corrections are annotations, never independently verified facts.
Invalidated outcomes leave retrieval. Any active annotation also removes the old runtime episode from ordinary memory recall.
Manual owner-authored notes remain independent records and require their own correction.

The planner is asked to return `Learning use: OUTCOME_ID: specific effect or rejection` lines.
A use record saves supplied references, their hashes, explicit reported effects, and the next proposed tool actions.
Missing explanation means `not_reported`, not inferred influence. Explanations are always `model_interpretation`.
The actual plan and receipts establish what ran. Reported influence does not establish a causal performance gain.
Uses survive restart, and repeated writes for one task step are idempotent.
All existing action approvals and budgets still apply.

## Owner API

Use an authenticated owner session. Writes require its CSRF token and configured origin.

| Endpoint | Purpose |
|---|---|
| `GET /api/outcomes?task_id=TASK_ID&offset=0` | Inspect evidence and note history, 100 records per page. Task filter is optional. |
| `GET /api/outcomes/OUTCOME_ID/evidence/SHA256` | Read the exact retained evidence version referenced by a plan. |
| `POST /api/outcomes/OUTCOME_ID/notes` | Add or explicitly supersede an owner annotation. |
| `GET /api/tasks/TASK_ID/learning` | Preview relevant records and inspect recorded planning use. |

Example note:

```json
{"kind":"invalidation","text":"The claimed deployment was only a draft.","source":"Owner inspection of deployment receipts","confidence":0.9,"supersedes":null}
```

Kinds are `interpretation`, `correlation`, `correction`, and `invalidation`.
For subsequent edits, set `supersedes` to the current note ID. Concurrent edits permit one winner.
Old notes remain inspectable. A correction can replace an invalidation when new evidence warrants reuse.
Confidence is owner-supplied, not calibrated probability. Repetition does not increase it.
The dashboard interface is unchanged; these controls use the authenticated API.

## Recovery and limits

SQLite schema 11, PostgreSQL schema 7, and portable backup format 7 include outcome evidence and planning use.
Portable restore accepts formats 1–6; missing outcomes are backfilled from available history.
SQLite migration accepts schemas 5–11. Stop services and make a private backup before upgrading.
Hashes detect accidental document corruption. Corrupt records or notes are excluded from recall.
They do not protect against a compromised host that rewrites both content and hashes.
Rollback should disable planning reuse through a counter-change that retains evidence and quality gates.

## Verification

`uv run pytest tests/test_learning.py tests/test_memory.py tests/test_quality.py -q` exercises actual runtime and SQLite storage.
Coverage includes failed attempts, cancellations, evidence, later planning, corrections, isolation, cost updates, and restoration.
`tests/test_infrastructure.py::test_learning_postgres_reuse_correction_and_restore` exercises real PostgreSQL and portable backups in CI.
All model answers in these tests are scripted. Live model learning and production performance remain unverified.
