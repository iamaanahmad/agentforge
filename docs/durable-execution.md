# Durable execution

The worker resumes safe interrupted tasks from durable checkpoints.
Completed write receipts survive restarts and matching model requests with different call IDs.
Unknown write outcomes remain failed for owner inspection. This is not universal exactly-once delivery.

## Execution contract

Schema version 4 adds `executions` and `plan_steps`; the execution format is version 1.
The journal stores revisions, sequential dependencies, action identities, observations, final text, and verification.
Task context and pending actions share a transaction with each plan checkpoint.
The model proposes the next tool plan after observing results. A failed read becomes an explicit observation.
The model can select another source, change its plan, or report the missing evidence.
This is incremental tool planning, not mission decomposition or a general dependency scheduler.

A normalized tool name and exact arguments identify each write within a task.
Identical writes reuse the first action ID and its exact approval, even with a new model call ID.
Intentional duplicate identical writes require separate tasks. Changed arguments require a new action and approval.
Reads retain distinct model call identities, allowing fresh observations.
Legacy call IDs preserve existing signed approvals. Reused IDs with changed content fail closed.

The authenticated task-detail API exposes `execution` and `plan_steps` for inspection.
Observations are private workspace data. Existing activity events show model steps and recovery.
There is no new timeline UI in this release.

## Crash boundaries

| Crash point | Recovery |
|---|---|
| Before model output is checkpointed | Repeat model reasoning within the saved step budget; no tools have dispatched. |
| After plan checkpoint, before registry intent | Resume the pending action under current policy and approval. |
| After registry intent, before request | An incomplete write is held. The runtime cannot prove dispatch did not happen. |
| After provider acceptance, before local receipt | Hold the ambiguous write for inspection. Never infer failure from a missing response. |
| After completed receipt, before observation checkpoint | Reuse the validated receipt and checkpoint its result. |
| After final text checkpoint, before task completion | Run execution verification without another model request. |

Resend requests include a stable `Idempotency-Key` derived from task and action IDs.
[Resend documents a 24-hour retention window](https://resend.com/docs/dashboard/emails/idempotency-keys).
The runtime does not automatically replay ambiguous email requests, even within that window.
No provider receipt lookup or reliable GitHub write read-back is implemented here.
Inspect provider records before creating replacement work. A new task gets a new idempotency key.

## Bounds and concurrency

SQLite immediate transactions serialize task claims and receipt reservations.
Per-task OS locks fence concurrent engine runners and prevent recovery from stealing an active run.
The SQLite worker retains its single-volume process lock. SQLite across hosts and NFS remain unsupported.
The optional [PostgreSQL path](distributed-infrastructure.md) uses session locks, expiring leases, and fenced transactions across workers.
Locks release when a process dies. Durable records survive; lock files contain no task content.

`A4G_MAX_RECOVERIES` defaults to 3; `A4G_MAX_MODEL_RETRIES` defaults to 2 transport retries.
Each model attempt consumes the existing step limit. Daily run limits also apply to resumed claims.
Interrupted reads allow at most three attempts for one call ID. New read plans still consume model steps.
Failed-output reads remain held; the planner may use a different source or report the limitation.
`A4G_TASK_TIMEOUT_SECONDS` defaults to 3600 elapsed seconds from the first checkpoint, including approval wait time.
Tool and provider timeouts remain cooperative. Cancellation blocks future dispatch, not an already active HTTP request.
An accepted write after cancellation still records its receipt, without completing the cancelled task.

## Verification and evidence

Final checks require nonempty final text, observed plan steps, no pending actions, and no ambiguous writes.
These checks establish execution integrity. They do not independently validate factual accuracy or task success.
A result can honestly report a failure or missing evidence. Independent critics belong to a separate capability.

`tests/test_durable_execution.py` covers crash boundaries, changed model IDs, dependencies, concurrent runners,
cancellation, recovery and retry budgets, deadlines, replanning, and completion checks.
Release restoration tests cover signed approvals and artifacts across database backups.
Service and model responses in these tests are scripted. No live email, GitHub write, or model completion is claimed.

## Migration and rollback

Stop web and worker, take a SQLite backup, then start the upgraded release.
Existing tasks and owner records remain intact. Do not delete started receipts to force a retry.
For rollback, stop both processes and restore the previous code; additive journal tables can remain.
Keep the newer database to preserve completed action receipts. Do not restore an old snapshot over external actions.
