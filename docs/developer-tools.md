# Developer tools and recovery

Agent4Good ships the `a4g` CLI, a synchronous Python SDK, and an authenticated `/api/v1` contract.
These use the same mission, approval, budget, and cancellation controls as the dashboard.
The original `/api` endpoints remain compatible. No separate API token bypass exists.

## Install and run

Use Python 3.12 or 3.13. From a fresh checkout:

```sh
uv sync --frozen --group dev
uv run a4g init
uv run a4g config-check
uv run a4g migrate
uv run a4g serve
```

In a second terminal in the same directory:

```sh
uv run a4g worker
```

`init` creates a mode-600 `.env` with generated owner and session secrets. It refuses an existing file.
This configuration serves loopback HTTP only. Read the private `.env` locally for the owner password.
For production, configure HTTPS, secure cookies, allowed hosts, and external vault keys. See [deployment](deployment.md).
Use a process supervisor for persistent installations. Foreground commands stop when their terminal closes.

For an installed package without the checkout:

```sh
uv build
uv venv /tmp/a4g-installed
uv pip install --python /tmp/a4g-installed/bin/python dist/agent4good-0.1.0-py3-none-any.whl
/tmp/a4g-installed/bin/a4g --help
```

The wheel includes the server, CLI, SDK, dashboard, and role playbooks.
The source distribution includes these instructions and versioned examples.

## Authentication and stable API

Remote CLI commands prompt for the owner password using hidden input.
Alternatively, supply `--password-file /private/owner-password` or `A4G_ADMIN_PASSWORD_FILE`.
`A4G_ADMIN_PASSWORD` also works. Passwords never belong in command arguments or shell history.
Set `A4G_URL=https://your-host` or pass `--url` before the command.
The SDK refuses remote plaintext HTTP, embedded credentials, and redirects.

1. `POST /api/v1/login` with `{"password":"…"}` returns a session cookie and `csrf_token`.
2. Keep the cookie and pass `X-CSRF-Token` on every write, using `application/json`.
3. `POST /api/v1/logout` revokes the session. CLI and SDK context managers log out automatically.

Sessions last at most 12 hours. Credentials and sessions remain in memory in the SDK.
The CLI opens one session per command and never stores tokens on disk.
Login throttling, host checks, origin checks, credential-content denial, and exact approval signatures still apply.

`a4g schema` retrieves the authenticated OpenAPI document at `/api/v1/openapi.json`.
Version 1 supports missions, tasks, approvals, ordered timeline pages, debugging, replay, and readiness.
Existing fields and semantics remain stable within v1; additive fields may appear.
A breaking request or response change requires a new API version.
This first contract serves the existing single owner, not multiple users or tenants.

| Intent | API |
|---|---|
| Create / inspect mission | `POST /api/v1/missions`, `GET /api/v1/missions/{id}` |
| Plan / review mission | `POST /api/v1/missions/{id}/plan`, `POST /api/v1/missions/{id}/review` |
| Start / pause / resume / cancel / replan | `POST /api/v1/missions/{id}/control/{action}` |
| Inspect task | `GET /api/v1/tasks/{id}` |
| List / decide approval | `GET /api/v1/approvals`, `POST /api/v1/approvals/{id}/decision` |
| Observe events | `GET /api/v1/timeline?after=0&limit=100` |
| Inspect saved execution | `GET /api/v1/tasks/{id}/debug` |
| Replay saved evidence | `GET /api/v1/tasks/{id}/replay?mode=recorded&offset=0&limit=100` |
| Configuration detail | `GET /api/v1/readiness` |
| Execution readiness | `GET /api/v1/health` |

## Mission CLI and SDK

The example [examples/v1/mission.py](../examples/v1/mission.py) creates, inspects, and cancels a tagged draft.
It requires `A4G_ADMIN_PASSWORD` and optionally `A4G_URL`. No model key is needed for this draft journey.
Starting a mission does require the configured planning model.

Prepare a mission JSON file using the v1 schema, with a future timezone-aware deadline.
The mission contract includes an objective, criteria, allowed tools and roles, permissions, and budgets.
See [missions](missions.md) for complete semantics.

```sh
a4g mission create mission.json
a4g mission inspect mission_ID
a4g mission plan mission_ID plan.json
a4g mission start mission_ID
a4g events --task-id task_ID --after 0
a4g approvals list
a4g approvals decide approval_ID approve
a4g mission cancel mission_ID
```

Inspect the exact approval arguments before approving. A decision can resume a worker and cause the approved external effect.
Approval decisions never directly dispatch tools. Existing policy checks still run during dispatch.
Cancellation stops future work; an external request already active may still finish.

```python
import os
from agent4good.sdk import Client

with Client(os.environ["A4G_URL"]) as client:
    client.login(os.environ["A4G_ADMIN_PASSWORD"])
    mission = client.create_mission(spec)  # v1 mission dictionary
    saved = client.mission(mission["id"])
    client.cancel_mission(mission["id"])
```

Use `plan_mission`, `review_mission`, `control_mission`, `events`, `approvals`, and `decide` for further controls.
`events` returns ordered pages. Reuse `through` and `next_cursor` to export a stable event window.
For live observation, request a new window after the previous one finishes. No background polling runs implicitly.
The SDK raises `APIError` with an HTTP status and safe guidance. It never automatically retries writes.
After a timeout, inspect saved state before retrying creation or an approval.
Mission creation has no idempotency-key contract; a blind retry can create another draft.

## Debugging and replay

```sh
a4g debug task_ID
a4g replay task_ID
a4g replay task_ID --offset 100 --limit 100
```

Replay reads recorded plan steps, observations, completed or uncertain receipts, checkpoints, and final output.
It does not call models, tools, or providers. It does not change task status or approval records.
Known credentials are redacted. Raw prompts, provider transcripts, and pending tool arguments are excluded from this projection.
Receipt and observation text is capped at 20,000 characters per row, with a truncation marker.
Pages contain at most 100 rows of each collection. Replay pages reflect current records, not a historical time-travel snapshot.
Use the timeline export for a fixed event boundary. Private business data remains private even after credential redaction.

Only `recorded` mode exists. Other modes and POST requests are rejected.
There is deliberately no effectful replay command. To do new work, inspect provider receipts, then create a new draft.
Starting new work uses current policies, permissions, and fresh exact-action approvals as applicable.
A new draft does not inherit the old task's approvals or receipt deduplication boundary.
A `started` receipt is uncertain. Replay never converts it into success or retries the effect.

## Configuration and health

`a4g config-check` validates settings, secret-file access, vault keyring placement/permissions, and configured broker sockets.
It prints field guidance without printing credentials and returns nonzero for invalid configuration.
It does not prove provider access, database connectivity, bucket permissions, or available funds.
`a4g readiness` provides authenticated deployment detail and model-route availability.
`a4g health` returns nonzero unless storage, a recent worker heartbeat, and the planning model configuration are available.
PostgreSQL checks count live worker rows. SQLite checks a valid heartbeat within 30 seconds.
A future or malformed timestamp never establishes readiness.
`/healthz` remains the storage liveness endpoint for existing supervisors.
Provider configuration is not a successful live model call. Readiness makes no production reliability claim.

## SQLite migration, backup and restore

```sh
a4g schema-status
a4g backup /private/backups/snapshot.sqlite3
# Stop BOTH web and worker before migration.
a4g migrate --backup /private/backups/before-upgrade.sqlite3
# Stop both services; choose a NEW data directory.
A4G_DATA_DIR=/private/recovered a4g restore /private/backups/snapshot.sqlite3
```

An existing database requires an exclusive new backup before migration. Newer schemas are refused, never downgraded.
The current SQLite schema is 11. Startup retains the existing additive migration path for older databases.
The migration command refuses an active worker lock. You must also stop the web process.
There is no online schema migration or automatic rollback promise.
Backups use SQLite's consistent backup API, integrity checks, and mode-600 exclusive creation.
They include private workspace data and encrypted credentials; keep backups encrypted off-host.

Restore accepts only a new database destination. It never overwrites the current database or starts services.
It preserves tasks, approvals, artifacts, and receipts, while removing sessions and stale worker readiness.
Restore the matching external keyring separately, keeping it outside the data directory.
Keep the same tenant/environment values, then inspect receipts and schedules before starting the recovered worker.
A backup can predate a successful external write. It cannot undo that write or prove it never happened.
Never run original and restored workers together. Rotate the session secret as part of host recovery.
Use a new data directory and the previous release for rollback; preserve the failed installation for inspection.

## PostgreSQL transfer

The installed commands wrap the existing [distributed transfer contract](distributed-infrastructure.md):

```sh
a4g backup-postgres /private/backups/workspace.json
# Stop all control/worker replicas; use an empty destination.
a4g restore-postgres /private/backups/workspace.json
a4g migrate-postgres /private/source.sqlite3 --backup /private/backups/sqlite-transfer.sqlite3
```

Configure PostgreSQL, object storage, tenant, and environment for the destination.
Transfers verify table and object content, preserve execution evidence, and refuse nonempty destinations.
The portable backup includes object content. External vault keys remain separate.
Schema initialization uses the existing PostgreSQL startup migration contract.
Stop all services for transfer and restore; a CLI process cannot stop remote replicas for you.
Sessions and stale heartbeat settings are cleared after successful restoration.
The existing scripts remain available for older operations guides.

## Acceptance and limits

```sh
uv run pytest -q tests/test_developer_tools.py
uv build
uv venv /tmp/a4g-installed
uv pip install --python /tmp/a4g-installed/bin/python dist/agent4good-0.1.0-py3-none-any.whl
python3 scripts/developer_acceptance.py --python /tmp/a4g-installed/bin/python
```

The harness runs outside the checkout with fresh settings and real server/worker processes.
It checks both interfaces, cancellation, replay, readiness, migration, backup, restoration, and overwrite refusal.
All drafts are cancelled. Processes and disposable data are removed, including on failure.
CI repeats wheel installation on Python 3.12 and 3.13 and runs PostgreSQL checks against real service containers.
Tests use no live model credentials, external messages, production data, or paid actions.
Useful model-generated mission completion remains a separate live acceptance requirement.
