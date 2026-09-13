# Agent4Good

A self-hosted AI growth and product operator. Give it an outcome, choose an agent, and review the work in one private workspace.

**Status: working v0.1 release candidate for one owner, with SQLite or an optional PostgreSQL worker stack.** This is an original implementation, not a copy of Tin's private platform. Production deployment requires your own API credentials, HTTPS host, backups, and a live acceptance test. Nine specialist roles are included. A bounded worker pool runs delegated child tasks concurrently; PostgreSQL permits worker replicas.

## What works

- A responsive dashboard with missions, tasks, results, decisions, nine agents, shared memory, schedules, connections, and activity.
- An OpenAI Responses or Anthropic Messages tool loop with versioned plans, checkpoint recovery, bounded retries, and execution-integrity checks.
- Stable write identities reuse completed receipts across replanning. Ambiguous writes stop for inspection.
- Original, editable role playbooks for strategy, research, product engineering, analytics, SEO, support, outreach, paid acquisition, and finance.
- Owner-controlled manual, supervised, and autonomous modes. Scheduled autonomous work can run without an open browser.
- Exact approval for every external write. The owner sees the recipient, message, branch, file content, or issue before execution.
- Persistent tool receipts. Duplicate call IDs reuse a completed result. Interrupted or ambiguous calls fail for inspection, never silently retry.
- Saved Markdown/text/code artifacts, [layered memory with bounded retrieval](docs/layered-memory.md), a task activity trail, and consistent SQLite backup tooling.
- Optional GitHub, Brave Search, HTTPS website reading, and Resend adapters. Missing credentials remove tools from the model's available tool list.
- Private owner login, expiring HttpOnly sessions, CSRF and origin checks, login throttling, request size limits, host checks, and a strict content security policy.
- Docker deployment files, a separate supervised worker, health probes, automated tests, and GitHub CI.

## Capability boundaries

| Area | Included now | Not implemented in v0.1 |
|---|---|---|
| Product work | Approved offline coding sandboxes, repository reads, branches, commits, draft PRs, checked merges, and configured workflow dispatch | Unrestricted shell, network package installation, full Dockerfile builds, production sandbox hosting |
| Research and SEO | Brave search, allowlisted public pages, source-based reports and content drafts | Search Console, rank tracking, backlink databases, automatic CMS publishing |
| Analytics and finance | Analysis of supplied evidence, original playbooks | PostHog, GA4, Stripe, database access, session replay |
| Support and sales | Supplied-ticket analysis, exact approved Resend email sends | Inbound inbox sync, CRM, bulk sequences, LinkedIn |
| Paid growth and creative | Campaign plans, copy and creative briefs | Ad activation, image/video generation, real budget management |
| Browser work | Exact-approved isolated Chromium journeys, forms, file transfer, tabs, screenshots, encrypted task sessions | Unrestricted browsing, challenge bypass, cross-task session sharing, production browser hosting |
| Memory | Private working snapshots, past-run episodes, sourced facts, bounded lexical retrieval, versioned corrections and quarantine | Embedding search, automatic document ingestion, multi-user memory |
| Missions | Immutable objectives and criteria, bounded dependent plans, shared limits, revisions, pause/cancel, receipt and owner evidence checks | Automatic mission-wide semantic acceptance |
| Quality | Owner-defined executable checks, isolated critics, bounded builder revisions, separate verifiers, saved evidence | Visual critics, proof of arbitrary claims, live model quality evidence |
| Scheduling | One-time, daily time-zone recurrence, deadline, event, and condition triggers; dependencies, priorities, atomic occurrence receipts | Cron expressions, arbitrary code or network conditions, hard completion deadlines |
| Agent operations | Nine executable specialist roles, bounded child workers, durable messages and contexts, tasks, schedules, memory, approvals | Plugins/MCP, multi-tenant SaaS, SSO |

Evidence for the included paths: [API checks](tests/test_app.py), [engine checks](tests/test_engine.py),
[tool checks](tests/test_tools.py), and [provider contract checks](tests/test_provider.py).
[Release acceptance checks](tests/test_release_acceptance.py) restore a real SQLite backup into a separate workspace.
They verify saved approvals, artifact retrieval, safe checkpoint recovery, and refusal to replay ambiguous writes.
Model responses in these tests are scripted. Live model completion and production operation remain unverified.
[Isolated coding acceptance](docs/isolated-coding.md) adds real container and GitHub delivery evidence.
The missing capabilities above remain planned, not available integrations.

Roles describe how an agent works. They do not manufacture access to a service. Do not grant this worker an unrestricted shell or mount a Docker socket to fill those gaps.

See [isolated coding workspaces](docs/isolated-coding.md) for broker setup, threat model, resource limits, and delivery rules.

## Run locally

Requirements: Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/). Node is only used for the optional JavaScript syntax check.

```sh
uv sync --frozen --group dev
uv run python scripts/bootstrap.py
# Read .env locally for your generated workspace password.
# Add A4G_OPENAI_API_KEY in .env to enable real model runs.
uv run uvicorn agent4good.app:create_app --factory --host 127.0.0.1 --port 8000
```

In a second terminal:

```sh
uv run python -m agent4good.worker
```

Open `http://localhost:8000`. You can save drafts, memory, and schedules without provider keys. Starting an AI task requires the key for its selected provider. There is no mock success mode in the application. Tests use explicit fake providers and cannot send email or spend money.

Configure optional services using `.env.example`. Restart **both** processes after changing server configuration. Secrets never belong in task instructions, memory, screenshots, or git. Rotate the session secret to invalidate all existing sessions.

## First real task

1. Add your product description in Memory under `product`.
2. Set your goal in Settings. Keep supervised mode while learning the workflow.
3. Configure your model provider using the server environment. A configured flag does not prove the key is valid.
4. Create a small task, such as: “Use my product note to draft three positioning options. Save a Markdown report.”
5. Read the result and artifact. For connected actions, inspect the exact values in Decisions before approving.
6. Set provider-side spending limits before enabling recurring autonomous runs.

OpenAI calls use [the Responses API with function tools](https://developers.openai.com/api/docs/guides/function-calling). Set `A4G_MODEL` to a Responses-compatible model available to your account. Model/API changes may require adapter updates.

## How autonomy works

```text
Owner task or schedule → durable queue → worker claims task → model proposes tools
                                       ↓
                      server validates schema and permissions
                        ↓                            ↓
                  allowed read                  external write
                        ↓                            ↓
                 execute and record          exact owner approval
                        └───────────────┬────────────┘
                                  next model step
                                        ↓
                              result + saved artifacts
```

| Mode | Starting work | Tool decisions |
|---|---|---|
| Manual | Owner starts; schedules create drafts | Owner approves every tool call |
| Supervised (default) | Owner starts; schedules create drafts | Reads and artifacts run; external writes and memory changes need approval |
| Autonomous | Owner starts or schedules enqueue | Same write approvals; unattended model/search calls can incur costs |

Approval is bound to the task, call ID, tool name, and exact arguments. A rejection stops that task. Changing autonomy never removes approval for external writes. Stop prevents future steps; an external request already in flight can still finish.

The daily run limit includes resumed approval segments and resets at UTC midnight. The step limit applies across the whole task, including pauses. These are workload limits, **not a dollar cap**. Configure billing limits with each provider.

## Deploy

See [deployment and operations](docs/deployment.md). The Docker Compose stack includes a non-root web process, a non-root worker, persistent storage, health checks, and optional Caddy TLS.

```sh
# Configure .env for your real domain and secrets first.
docker compose --profile tls up --build -d --wait
```

Do not publish the local development configuration. No production host or customer provider credential is bundled with this repository.

## Verify and develop

```sh
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
node --check agent4good/static/app.js
uv build
```

Tests cover API authentication, CSRF, limits, persistence, approvals, task cancellation, scheduler behavior, worker recovery, tool receipts, SSRF controls, and repository restrictions. CI also builds and starts the Docker services. Live provider acceptance needs your own credentials and explicit test scope.

- [Architecture and extension guide](docs/architecture.md)
- [Deployment and recovery](docs/deployment.md)
- [Security model](SECURITY.md)

## Ownership

The code is committed to your repository. No third-party open-source license is selected on your behalf. Choose a license before offering redistribution rights. Provider SDKs and dependencies retain their own licenses.

## Typed tool execution

All 12 implemented tools now share validated execution, exact approvals, durable receipts, and shared rate limits.
The authenticated `/api/tools` catalog separates executable tools from planned categories.
See [tool contracts and bounded live evidence](docs/tool-registry.md).

## Scoped action policies

Owner-configured rules now restrict tool execution by user, role, task, tool, environment, and action class.
Existing exact write approvals remain mandatory. Shared limits reserve attempts, recipients, and configured cost ceilings.
See [policy configuration and limits](docs/action-policies.md). Actual provider billing and multi-user operation remain outside this scope.

## Credential and environment protection

An encrypted vault now brokers credentials by owner, agent role, adapter purpose, tenant, and environment.
Keys remain outside the data volume. Signed webhooks can create drafts, with replay and rate checks.
Existing owner deployments remain supported; multi-user and arbitrary command execution remain unavailable.
See [migration, key rotation, and the threat model](docs/credential-security.md).

See [durable execution](docs/durable-execution.md) for crash boundaries, budgets, and evidence limits.

## Model routing

Assign independent models to planning, coding, browsing, research, summarization, and verification.
OpenAI Responses and Anthropic Messages share a normalized tool contract.
Each task pins its profile across recovery; failures never switch providers automatically.
Connections shows all six routes, missing keys, and recorded call evidence.
See [configuration, budgets, capability limits, and acceptance evidence](docs/model-routing.md).

## Distributed deployment

Use [the PostgreSQL deployment guide](docs/distributed-infrastructure.md) for independent control and worker containers, a database queue, private objects, lease recovery, and versioned migration. The original SQLite commands remain single-host only. Real service tests use scripted model answers; public production operation remains unverified.

Browser setup, supported actions, network limits, and recovery: [Browser control](docs/browser-control.md).

## Executable specialists

[Specialist workers](docs/specialist-workers.md) adds bounded parallel delegation, private and shared context, durable messaging, priority, result collection, inherited permissions and cancellation. Real daemon restart acceptance uses scripted model responses; live provider completion remains unverified.

[First-class missions](docs/missions.md) turn an objective into dependent worker tasks. Mission success requires evidence against original criteria.

Scheduling behavior, APIs, migration, and dispatch guarantees: [dependency-aware scheduling](docs/scheduling.md).

## Independent quality checks

Critical tasks can require separate critic and verifier workers against owner-defined evidence checks.
Failed checks trigger bounded revisions; missing evidence cannot pass. Configure task contracts or server defaults through [the quality guide](docs/independent-quality.md).
Existing tasks keep their prior behavior. Scripted acceptance proves runtime boundaries, not live model correctness.
