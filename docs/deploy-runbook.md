# Deploy runbook

Use this checklist for one owner, one web process, and one worker on a persistent SQLite volume. [Deployment and operations](deployment.md) has the full recovery procedure. Render and Fly create billable services and storage; check the charges before launch.

## Before you start

1. Choose [Render](../render.yaml), [Fly](../fly.toml), or [production Compose](../compose.production.yaml). Keep exactly one SQLite machine or host. Do not enable idle suspension for scheduled work.
2. Prepare a public HTTPS address. For Compose, point its DNS record to the host and open ports 80 and 443. Render and Fly provide HTTPS addresses.
3. Create a persistent volume at `/app/data`. Render and Fly configs mount one. Compose creates `operator-data`. Keep the volume across updates.
4. Enter secrets in the host's private secret settings, or in a Compose `.env` file with mode `600`. Never commit secret values. Use new secrets for production.

## Required settings

| Name | Purpose | Where to set it |
|---|---|---|
| `A4G_ADMIN_PASSWORD` | Owner sign-in password, at least 16 characters. | Enter as a private secret on every host. |
| `A4G_SESSION_SECRET` | Signs sessions, at least 32 characters. | Render generates it. Set it as a private secret on Fly and Compose. |
| `A4G_PUBLIC_ORIGIN` | Exact public HTTPS origin, without a path. | Render supplies its service URL. Fly derives its app URL. Set it for Compose or a custom domain. |
| `A4G_ALLOWED_HOSTS` | Exact public hostname plus `localhost` and `127.0.0.1` for health checks. No wildcard. | The Render and Fly launcher derives it from the origin. Set it for Compose. |
| `A4G_SECURE_COOKIES` | Requires HTTPS sessions. | The host configs set `true`. Keep `true` in Compose. |
| `A4G_DATA_DIR` | Shared SQLite data directory. | The host configs set `/app/data`. Compose sets it for web and worker. |
| `A4G_ENVIRONMENT` | Production security domain. | The host configs set `production`. Compose sets it for web and worker. |

The app also requires a model provider for useful work. The default model uses `A4G_OPENAI_API_KEY`. Enter your own key as a private secret. For another provider, configure its credential and `A4G_MODEL_PROFILES` and `A4G_MODEL_ROUTES` as described in [model routing](model-routing.md). A key is not required for the web process to start, but model tasks cannot run without a working route. Set provider budgets before the first task.

`A4G_TENANT_ID` defaults to `default`. Set a stable value before first use if you need a different tenant label. Changing the security domain later needs a planned migration. `A4G_ISOLATION_MODE` supports `single_owner` only. Do not use these settings to share one installation among owners.

## Start and check

| Host | Start | Health and worker check |
|---|---|---|
| Render | Review cost, then create the Blueprint from the README button. Add the two prompted secrets before confirming. | Open `https://<your-service>/healthz`, then sign in and check the worker indicator. |
| Fly | Set the app name in `fly.toml`. Create one `agentforge_data` volume in `iad`, set the three secrets, then deploy. | Open `https://<your-app>.fly.dev/healthz`, then sign in and check the worker indicator. |
| Compose | Copy `.env.example` to `.env`, set the required values, then run `docker compose -f compose.production.yaml up --build -d --wait`. | Run `docker compose -f compose.production.yaml ps` and `docker compose -f compose.production.yaml exec -T worker python -m agent4good.healthcheck`. Check the HTTPS `/healthz` URL. |

`/healthz` checks the web process and SQLite. It does not prove that the worker or model provider works. After sign-in, run a small internal task, inspect its result, and retrieve its saved artifact. Test each external adapter separately before relying on it.

## Back up and recover

Create a consistent SQLite backup with `python scripts/backup.py <backup-path>` inside the running deployment. For Compose, use the [tested command and restore steps](deployment.md#backups). Copy the backup to encrypted storage off the host. Restore it into a separate disposable deployment and confirm sign-in and saved work. Stop web and worker before replacing a database. Preserve the old volume, inspect external action receipts, and avoid running original and restored workers together.

Optional GitHub, search, mail, browser, sandbox, PostgreSQL, S3, and vault settings are documented in [.env.example](../.env.example) and the linked feature guides. They are not required for this single-host startup. PostgreSQL requires a different deployment path. The optional MinIO images are not part of this stack.
