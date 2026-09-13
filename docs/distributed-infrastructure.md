# Separate control and worker infrastructure

Agent4Good has an optional PostgreSQL deployment path with independent worker processes and private object storage.
The original SQLite setup remains supported on one host with one worker.
Both paths still serve one owner. Worker replicas do not create child agents or a multi-user service.

## What runs

| Service | Responsibility | Persistent state |
|---|---|---|
| Control | Owner login, tasks, approvals, artifact downloads, status API | PostgreSQL |
| Workers | Claim tasks, run tools, checkpoint results, recover interrupted work | PostgreSQL and object storage |
| PostgreSQL 17 | Application records, durable queue, budgets, leases, receipts | Database volume |
| MinIO | Private text artifacts with verified content hashes | Object volume |

PostgreSQL is the durable queue. Enqueue and task state share one database transaction.
There is no Redis delivery step that could succeed independently of task persistence.
Workers poll the queue. Each worker executes one task at a time.
Scale the worker service only in this PostgreSQL configuration.

## Ownership, recovery, and limits

Claims atomically change task status and record a monotonic ownership number with an expiry.
Database time controls lease expiry. Heartbeats renew ownership every five seconds, only while the lease remains valid.
Every runner transaction checks ownership before work and before commit. A stale runner cannot save later results.
A PostgreSQL session advisory lock prevents another runner entering the same task concurrently.
Recovery requires both an available session lock and an expired lease.
Workers periodically inspect interrupted tasks. Safe checkpoints resume within the configured recovery budget.
Incomplete write receipts remain failed for inspection. Completed receipts remain reusable.

External systems cannot enforce our ownership number. An already-dispatched request can finish after lease loss or cancellation.
Its incomplete write receipt blocks automatic replay. This is not an exactly-once guarantee across external providers.
Loss of the database can also lose an advisory lock. The persisted lease and receipt checks remain necessary.
Use direct PostgreSQL connections or session pooling. Transaction pooling cannot preserve session advisory locks.

Short control transactions use one database advisory lock to preserve existing budget and policy rules.
Provider and tool calls run outside that transaction. Different tasks can execute those calls concurrently.
This design scales execution concurrency, not unlimited database throughput. No throughput benchmark or multi-region failover is claimed.

`A4G_MAX_CONCURRENT_RUNS` defaults to 4 across all workers.
`A4G_MAX_QUEUED_TASKS` defaults to 1,000. Queue admission returns HTTP 429 when full.
The existing daily run and model budgets still apply across workers.
`A4G_WORKER_LEASE_SECONDS` defaults to 60 and accepts 15–300 seconds.
Keep the lease longer than several heartbeat intervals.

SIGTERM stops new claims and drains the current task while heartbeats continue.
Compose allows 120 seconds before forced termination. Longer work then follows the normal recovery rules.
Adjust the grace period to match allowed task duration when full draining is required.

## Start an isolated installation

Use Docker Engine with the Compose plugin. Do not reuse production volumes for acceptance tests.

```sh
python3 scripts/bootstrap_distributed.py
sudo chown -R 10001:10001 .infra-secrets/keys
docker compose -f compose.distributed.yaml up --build -d --scale worker=2 --wait
curl --fail http://localhost:18000/healthz
docker compose -f compose.distributed.yaml exec -T control python scripts/infrastructure.py status
```

The generator refuses existing directories. It never prints credentials or overwrites an installation.
Retrieve the owner password privately from `.infra-secrets/admin_password` when signing in.
Secret files sit behind a host directory with mode 0700. Individual infrastructure mounts are read-only.
The encryption key file separately requires mode 0600 and container UID 10001 ownership.
Back up that external key separately from database and object backups.
All replicas must use the same owner secrets, vault keys, tenant, environment, and model configuration.
The keyring enables encrypted credential storage. It does not grant provider access or supply model credentials.

The control port binds only to `127.0.0.1:18000`. Database and object ports are not published.
Worker containers have no shared filesystem or Docker socket. Their temporary files can be discarded.
Application object credentials can read and write one bucket. They cannot administer users or delete objects.
Root object credentials are mounted only into the object server and its initialization job.

This example starts a local acceptance installation, not public HTTPS.
For production, put the control service behind TLS and set these explicitly:

```sh
export A4G_PUBLIC_ORIGIN=https://your-agent-domain
export A4G_SECURE_COOKIES=true
export A4G_ALLOWED_HOSTS='["your-agent-domain","localhost","127.0.0.1"]'
```

Supply a connected host, domain, private network, encrypted disks, off-host backups, and monitored service capacity.
For separate machines, supply a shared PostgreSQL URL and S3-compatible endpoint through private secret mounts.
Use TLS for traffic crossing hosts. The internal plaintext endpoints in Compose are for its local network.
Configure PostgreSQL replication and storage redundancy through your chosen host provider before claiming high availability.
Review MinIO's license and current security releases before choosing it for commercial production hosting.
An existing S3-compatible service can replace the example storage server.

The repository has no configured production target. Container acceptance does not establish a public deployment.

## Configuration

| Setting | Purpose |
|---|---|
| `A4G_DATABASE_URL` or `A4G_DATABASE_URL_FILE` | PostgreSQL connection URL; omit for SQLite |
| `A4G_ADMIN_PASSWORD_FILE`, `A4G_SESSION_SECRET_FILE` | Private owner bootstrap mounts |
| `A4G_S3_ENDPOINT`, `A4G_S3_BUCKET`, `A4G_S3_REGION` | Private object destination |
| `A4G_S3_ACCESS_KEY_FILE`, `A4G_S3_SECRET_KEY_FILE` | Scoped storage credential mounts |
| `A4G_CREDENTIAL_KEY_FILE` | External provider-vault encryption keyring |
| `A4G_INFRA_SECRET_DIR` | Compose host mount directory, default `.infra-secrets` |

S3 credentials can use corresponding direct settings or the standard AWS credential chain when direct values are omitted.
Never put credentials in tasks, logs, URLs shown to users, or source control.
The application redacts configured infrastructure credentials from saved model output and errors.
The services still share one trusted owner boundary. This is not a sandbox for hostile generated code.

## Migrate SQLite schema 5, 6, or 7

Stop the source web service and worker before migrating. Do not move real production data without owner authorization.
Keep the original volume unchanged. Start only PostgreSQL and object storage at the empty destination.
Configure target secrets for the same tenant and environment. Preserve the existing vault keyring and session signing secret.
Run the following from a private maintenance process with PostgreSQL settings:

```sh
uv run python scripts/infrastructure.py migrate-sqlite /private/source.sqlite3 /private/pre-migration.sqlite3 --source-stopped
```

The command takes a consistent private SQLite backup and checks database integrity and foreign keys.
It accepts schema 5, 6, or 7. Upgrade an older source through the supported SQLite release before migration.
It copies all application tables, compares every restored row, and restores database identity sequences.
The target must be empty except untouched default settings and policy. Any conflict rolls back the database copy.
Saved approvals, ciphertext, notes, tasks, model routes, receipts, and historical artifacts remain available.
Historical artifact text stays inline. New artifacts use object storage.
Running source tasks recover through the existing receipt rules after target workers start.
Do not run source and destination workers together.

## Backup and restore

Use a private directory outside the source data volume:

```sh
uv run python scripts/infrastructure.py backup /private/backup-v1.json
uv run python scripts/infrastructure.py restore /private/backup-v1.json
```

These commands use PostgreSQL settings. The old `scripts/backup.py` remains SQLite-only.
The versioned backup includes all application records and every referenced object, checked by content hash.
An exclusive filename prevents overwriting a backup. Files use mode 0600 and contain private workspace data.
The current portable implementation holds a control transaction while reading objects and materializes the backup in memory.
It is intended for bounded single-owner workspaces. Large stores need `pg_dump` plus an independently verified object snapshot.
A timeout fails the backup. Never interpret a failed or partial backup file as a valid recovery point.
Test restoration before relying on any backup method.

Restore requires a fresh database and the same tenant/environment prefix. A different empty bucket is allowed.
The original encryption keyring is required to decrypt restored credentials.
Object keys are content-addressed. Restore verifies their hashes before publishing database references.
Runtime heartbeats and ownership leases are not restored. Saved tasks resume through normal recovery checks.
An interrupted object upload can leave an unreferenced object. It does not publish an invalid artifact reference.
Object cleanup remains operator-owned; never delete a bucket to remove one unreferenced upload.

Copy backups and encryption keys to separate protected off-host destinations.
For large installations, pair PostgreSQL native backups with a consistent snapshot of referenced immutable objects.
The container checks also restore a real `pg_dump` into another PostgreSQL database.
A database backup cannot undo external actions. Inspect receipts before resuming old queued tasks or approvals.

## Monitoring and evidence

`GET /healthz` checks PostgreSQL and object-bucket access. It does not prove a worker is online.
The authenticated `GET /api/infrastructure` reports queue depth, running and failed tasks, online workers, and expired leases.
Each worker health probe checks its own container heartbeat. One healthy replica cannot mask another failed replica.
Worker start/stop records and task failures appear in the existing activity history.
Logs report heartbeat and execution interruptions without printing connection credentials.
Configure alerts for no online workers, persistent expired leases, growing queues, and failed tasks.
Database outages surface as failed probes and logs; an unavailable database cannot record its own outage.

Run the real-service acceptance suite against a disposable PostgreSQL database and object server:

```sh
A4G_TEST_DATABASE_URL=postgresql://test-owner@localhost/test_database uv run pytest tests/test_infrastructure.py -q
```

The tests create and remove isolated database schemas. They require schema creation privileges and test-only object credentials.
Defaults for object testing use port 59000 and explicitly named test credentials. Never point them at a customer bucket.
The suite covers competing claims, global admission limits, expired ownership, schedule races, and protected control access.
It kills a real worker process after an artifact receipt, waits for lease expiry, and verifies recovery without duplication.
It also checks graceful termination, SQLite migration, private object restoration, corruption refusal, and credential injection.
Models are scripted in these tests. PostgreSQL, object storage, and worker processes are real.
No live model completion, external write, multi-region resilience, or production operation is implied.

The `Distributed infrastructure` GitHub workflow runs real PostgreSQL and MinIO containers.
It separately starts the full Compose stack with two workers, private secret mounts, health probes, and backup restoration.
See [PostgreSQL locking](https://www.postgresql.org/docs/current/explicit-locking.html) and
[transaction behavior](https://www.psycopg.org/psycopg3/docs/basic/transactions.html) for the underlying storage contracts.

Layered memory uses PostgreSQL schema 3 and portable backup format 3.
Older backup formats 1 and 2 remain readable. See [memory migration](layered-memory.md).

## Mission persistence

Missions use PostgreSQL schema 4 and backup format 4. Formats 1–3 remain readable. SQLite migration accepts versions 5–8. See [mission limits and restoration](missions.md).
