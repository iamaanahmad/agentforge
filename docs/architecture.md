# Architecture

Agent4Good is a single-owner service with SQLite or PostgreSQL persistence.
The optional [distributed stack](distributed-infrastructure.md) separates control services, worker replicas, and object storage.

- `app.py` authenticates the owner, serves the dashboard, and exposes JSON endpoints.
- `worker.py` holds an OS file lock in SQLite mode or a PostgreSQL lease in distributed mode, recovers interrupted work, checks schedules, and claims one queued task at a time.
- `engine.py` runs the model/tool loop. Task context, pending calls, approvals, and tool receipts survive process restarts.
- `provider.py` implements OpenAI Responses and Anthropic Messages at fixed endpoints. `model_router.py` pins owner-selected profiles and reserves call budgets durably. Neither accepts a model-selected API host.
- `tools.py` owns typed tool contracts, discovery, adapters, approval enforcement, rate limits, and durable execution receipts.
- `catalog.py` and `playbooks/` supply the nine original role definitions.
- `db.py` selects SQLite or `postgres.py`; each uses short control transactions. Network requests happen outside database transactions.
- `static/` is dependency-free browser code. User/model text is escaped before rendering. Artifacts download as plain-text attachments.

## Task lifecycle

`draft → queued → running → done | failed | waiting_approval`

An approval moves `waiting_approval → queued`. Rejection or owner cancellation moves active work to `cancelled`. Closed tasks cannot restart. Create a new task after inspecting the earlier result.

A tool call receives a persistent `started` receipt before execution and a `done` receipt after a response. A completed action reuses its receipt, including matching writes with new model call IDs. Incomplete writes refuse execution. Bounded read retries can obtain a fresh observation. This prevents automatic replay; it does not promise distributed exactly-once delivery when a provider accepts a request but its response is lost.

Recovery requeues safe interrupted `running` tasks from saved checkpoints. Ambiguous writes and exhausted recovery budgets fail for inspection. Waiting approvals remain available. Schedules coalesce missed intervals into one task and advance from the current time. They do not replay every missed interval. The worker must be online; this service does not execute while the host is stopped.

## Adding a capability

1. Define input and output schemas plus a `ToolSpec` in `tools.py`. Add the bounded implementation behind registry dispatch.
2. Add server-owned credentials/configuration in `config.py`. Never ask the model to select credentials or a target account.
3. Restrict target hosts, repositories, or accounts in the adapter. Bound response sizes and timeouts.
4. Add the tool to `MUTATING` if it changes external state, contacts someone, incurs an action-specific charge, or changes a promise.
5. Expose only configured implementations. Declare scopes, risk, rate limits, deadlines, and audit requirements in the registry.
6. Test denied/approved/malformed/timeout/replay paths with fake services. Then perform a bounded live acceptance test.
7. Update the relevant original playbook and documentation. Do not claim a new skill works until the tool path does.

Coding execution uses a separate offline Docker broker through a private Unix socket. It has fixed resource caps and no host mounts. See [isolated coding](isolated-coding.md). Browser execution remains planned. Arbitrary model commands never run inside the API/worker container.

## Data and operations

SQLite WAL storage must live on a local persistent disk shared by the two processes. Do not use NFS or run multiple hosts against this database. SQLite schema version 5 adds model routing and call budgets. Version 4 added versioned execution journals, plan steps, and attempt counters. Schema version 3 added a persistent tenant/environment binding, encrypted credentials, and webhook receipts. Existing owner data migrates in place.

Records remain until the owner archives or removes the deployment data through a maintenance procedure. There is no automatic retention purge. Vault credentials are encrypted; other workspace records remain plaintext. Encrypt disks and backups. Private task content may be sent to the selected model provider; `store=false` does not override that provider's contractual retention policies.

See [the registry contract](tool-registry.md) for the public execution interface and tested limits.

`credentials.py` owns host-only credential storage and brokerage. `webhooks.py` verifies signed draft creation.
See [security and migration](credential-security.md) before changing an existing deployment.

`execution.py` checkpoints sequential plan revisions, dependencies, observations, and final execution checks.
See [durable execution](durable-execution.md) for the exact recovery contract.
