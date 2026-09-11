# Architecture

Agent4Good is a single-owner service with two processes and one persistent SQLite database.

- `app.py` authenticates the owner, serves the dashboard, and exposes JSON endpoints.
- `worker.py` holds an OS file lock, recovers interrupted work, checks schedules, and claims one queued task at a time.
- `engine.py` runs the model/tool loop. Task context, pending calls, approvals, and tool receipts survive process restarts.
- `provider.py` calls the OpenAI Responses API at a fixed endpoint. It never accepts a model-selected API host.
- `tools.py` defines strict tool schemas and fixed service adapters. The engine enforces approvals before invoking adapters.
- `catalog.py` and `playbooks/` supply the nine original role definitions.
- `db.py` owns schema initialization and short SQLite transactions. Network requests happen outside database transactions.
- `static/` is dependency-free browser code. User/model text is escaped before rendering. Artifacts download as plain-text attachments.

## Task lifecycle

`draft → queued → running → done | failed | waiting_approval`

An approval moves `waiting_approval → queued`. Rejection or owner cancellation moves active work to `cancelled`. Closed tasks cannot restart. Create a new task after inspecting the earlier result.

A tool call receives a persistent `started` receipt before execution and a `done` receipt after a response. A repeated completed call ID reuses the receipt. A repeated incomplete call refuses execution. This prevents automatic replay; it does not promise distributed exactly-once delivery when a provider accepts a request but its response is lost.

Recovery marks interrupted `running` tasks failed. Waiting approvals remain available. Schedules coalesce missed intervals into one task and advance from the current time. They do not replay every missed interval. The worker must be online; this service does not execute while the host is stopped.

## Adding a capability

1. Define an explicit function schema in `tools.py`. No generic arbitrary-URL authenticated HTTP or shell tool.
2. Add server-owned credentials/configuration in `config.py`. Never ask the model to select credentials or a target account.
3. Restrict target hosts, repositories, or accounts in the adapter. Bound response sizes and timeouts.
4. Add the tool to `MUTATING` if it changes external state, contacts someone, incurs an action-specific charge, or changes a promise.
5. Expose the tool only when configured. Describe what it actually does in Connections and the capability table.
6. Test denied/approved/malformed/timeout/replay paths with fake services. Then perform a bounded live acceptance test.
7. Update the relevant original playbook and documentation. Do not claim a new skill works until the tool path does.

Future browser or code-execution support needs a separate sandbox with resource limits, an egress policy, credential isolation, and a job protocol. It must not run arbitrary model commands inside the API/worker container.

## Data and operations

SQLite WAL storage must live on a local persistent disk shared by the two processes. Do not use NFS or run multiple hosts against this database. Schema version 1 is a new-install schema; future schema changes require explicit migrations and backup validation.

Records remain until the owner archives or removes the deployment data through a maintenance procedure. There is no automatic retention purge or encryption at rest. Encrypt disks and backups. Private task content may be sent to the selected model provider; `store=false` does not override that provider's contractual retention policies.
