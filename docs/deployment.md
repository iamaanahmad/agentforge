# Deployment and operations

## Deployment boundary

This guide covers the single-owner SQLite deployment on one Linux host.
For PostgreSQL, multiple workers, and private objects, use [the distributed deployment guide](distributed-infrastructure.md). It is not a multi-tenant service. Provide a persistent local volume. Do not expose development ports to the internet.

## Local acceptance

Run the commands in the README. Confirm `/healthz` returns 200, sign in, save a draft, and check the worker indicator. Without an OpenAI key, no model run starts. Set provider budgets before adding a key.
The [self-host guide](self-host-guide.md) walks through the first saved result before deployment.

## HTTPS deployment with Docker Compose

Use `compose.production.yaml` for the single-owner production stack. It runs the
web process, one worker, and Caddy. Both app processes share the `operator-data`
volume; Caddy keeps its certificates in `caddy-data`. PostgreSQL and the optional
MinIO images are not part of this stack.

1. Point your domain's DNS to your host. Allow inbound TCP 80/443 and optionally UDP 443 for Caddy.
2. Copy `.env.example` to `.env`, set permissions to `600`, and fill fresh strong secrets. Do not use the local bootstrap settings publicly.
3. Set `A4G_PUBLIC_ORIGIN=https://your-domain`, `A4G_SECURE_COOKIES=true`, and exact `A4G_ALLOWED_HOSTS`, including `localhost` for health checks.
4. Add your own model key. Add optional service keys only if needed. Use a fine-grained GitHub token for one repository.
5. Run `docker compose -f compose.production.yaml up --build -d --wait`.
6. Check `docker compose -f compose.production.yaml ps`, then run `docker compose -f compose.production.yaml exec -T worker python -m agent4good.healthcheck`.
7. Visit the HTTPS URL, sign in, and verify a small internal artifact task. Then test one approved disposable GitHub issue if that integration is enabled.
8. Configure host monitoring, encrypted backups, a provider budget, and your update routine before relying on unattended work.

Caddy terminates TLS. Uvicorn deliberately ignores forwarded client IP headers, so login throttling is shared per proxy IP. This is conservative for one owner. Do not enable arbitrary forwarded-header trust to bypass it. Restrict origin ports and deploy behind an access gateway if your risk profile requires it.

## Managed host paths: Render and Fly

The repository also includes `render.yaml` and `fly.toml`. Each runs the web
process and one SQLite worker on **one machine with one persistent volume**.
Their managed HTTPS endpoints replace Caddy. Neither config creates a PostgreSQL
service or uses the optional MinIO images. The host must stay running for
scheduled work, so do not enable idle suspension or scale above one machine.

**These are paid paths, not free production tiers.** Render Free web services
lose SQLite data on restart and do not allow a persistent disk. The Render
Blueprint selects a 2 GB paid web service and a 1 GB disk. Render listed the
service at $25/month and persistent disks at $0.25/GB-month in October 2026.
Fly bills running machines and volumes separately. Its October 2026 price
update lists $0.15/GB-month for volumes. Model provider calls can also incur
charges. Check current host prices before creating either service.

### Render

1. Review the cost and import this repository as a Render Blueprint. The
   `render.yaml` file creates one paid web service with a persistent disk.
   Automatic deploys are off. Creating the Blueprint starts billable service.
2. During the initial Blueprint setup, supply `A4G_ADMIN_PASSWORD` (at least
   16 characters) and `A4G_OPENAI_API_KEY` from your own account. Render
   generates `A4G_SESSION_SECRET`. Set `A4G_MODEL` and model budgets if you
   need values other than the defaults. Set optional adapter keys only for
   adapters you will use.
3. Render supplies the HTTPS origin to `A4G_PUBLIC_ORIGIN`. For a custom
   domain, set that variable to its exact HTTPS origin. The hosted launcher
   derives `A4G_ALLOWED_HOSTS` from the origin. Do not add a wildcard.
4. After deploy, check `/healthz`, sign in, and check the worker indicator.
   Then run the saved-result and backup checks below.

### Fly

1. Review Fly's current charges. Replace the placeholder `app` in `fly.toml`
   with your unique Fly app name. Create the app without deploying it yet.
2. Create one `agentforge_data` volume in the configured `iad` region. The
   `fly.toml` mount places it at `/app/data`. A volume is billable even when
   its machine stops. Keep exactly one app machine with this SQLite volume.
3. Set `A4G_ADMIN_PASSWORD` (at least 16 characters),
   `A4G_SESSION_SECRET` (at least 32 characters), and
   `A4G_OPENAI_API_KEY` as Fly secrets. Do not commit their values. Add
   `A4G_PUBLIC_ORIGIN` only for a custom HTTPS domain; otherwise the launcher
   uses `https://<app>.fly.dev`. Set optional keys and model budgets as needed.
4. Deploy only after checking the cost. Check `/healthz`, sign in, and check
   the worker indicator. Then run the saved-result and backup checks below.

Both hosts use the same `scripts/run_hosted.py` launcher. It stops both
processes if either exits. Their HTTP health checks only prove web and SQLite
readiness; inspect the worker indicator for worker health. Use an encrypted
off-host backup. A provider disk snapshot alone is not a verified SQLite
backup. Hosted startup and paid-provider behavior remain untested until an
owner creates a service.

Host terms checked October 12, 2026:
[Render Free limits](https://render.com/docs/free),
[Render disk limits](https://render.com/docs/disks),
[Render pricing](https://render.com/pricing), and
[Fly October 2026 pricing](https://fly.io/pricing-update/).

## Backups

Run an online consistent backup inside the deployment:

```sh
docker compose -f compose.production.yaml exec -T web python scripts/backup.py /app/data/backup-YYYYMMDD.sqlite3
```

Copy the backup to encrypted off-host storage using your host tooling. A file in the same volume is not disaster recovery. It contains private data and session hashes. Verify restore on a separate disposable deployment periodically.

Restore procedure:

1. Stop web and worker. Keep the previous database and volume as a recovery copy.
2. Validate the backup with SQLite `PRAGMA integrity_check`.
3. Replace `agent4good.sqlite3` with the backup. Remove old `-wal` and `-shm` files only while both services are stopped.
4. Preserve ownership for UID/GID 10001. Rotate `A4G_SESSION_SECRET` to invalidate restored sessions.
5. Start both services. Safe interrupted tasks resume. Inspect failed ambiguous writes and provider receipts before creating replacement work.

## Recovery and updates

- `failed` after restart: inspect the activity and service receipts. An email or GitHub change may already exist. Never assume it did not happen.
- `waiting_approval`: review the exact action in Decisions. Approval resumes the saved run.
- `queued` with worker online: the UTC daily run budget may be exhausted. It includes approval resumes. Check the server configuration and activity.
- `queued` with worker offline: restart the worker and inspect container health.
- Connection configured but HTTP error: check provider permissions, billing, rate limits, and resource scope. Configuration is not proof of access.
- Invalid host or sign-in failure: verify exact allowed hosts, HTTPS origin, secure cookies, and reverse-proxy routing.
- Duplicate worker: the second process exits because the file lock is held. Do not scale the SQLite worker service.

Before upgrades, back up the database and retain the prior image. Schema version 3 preserves owner data and binds the database to its configured tenant and environment. Existing sessions require fresh login. Read [vault migration notes](credential-security.md) before changing configuration.

## Go-live evidence

The repository's tests and container CI prove local code paths, not your live provider access or host reliability. Record a successful authenticated HTTPS session, worker heartbeat, artifact run, backup/restore, and any enabled external integration's bounded acceptance test before declaring your deployment production-ready.

## Repeatable recovery acceptance

Run `uv run pytest -q tests/test_release_acceptance.py` before releasing changes.
This invokes the backup command against tagged test data and restores into a separate temporary directory.
It checks owner authentication, exact saved approval, artifact retrieval, and completed receipts after restart.
It also checks that ambiguous writes stay failed and existing backup files cannot be overwritten.
Pytest uses disposable data; no external message is sent and no provider key is inherited by the backup process.

This is application integration evidence with scripted model responses, not a live deployment test.
A restored backup can predate external writes. Inspect provider receipts before resuming restored approvals or queued tasks.
Do not run the original and restored workers together. A database backup cannot roll back a provider action.
