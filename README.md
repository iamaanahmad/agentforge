# Agent4Good

A self-hosted AI growth and product operator. Give it an outcome, choose an agent, and review the work in one private workspace.

**Status: working v0.1 release candidate, built for one owner on one host.** This is an original implementation, not a copy of Tin's private platform. Production deployment requires your own API credentials, HTTPS host, backups, and a live acceptance test. Nine specialist roles are included; they share one sequential worker.

## What works

- A responsive dashboard with tasks, results, decisions, nine agents, shared memory, schedules, connections, and activity.
- A real OpenAI Responses API tool loop with durable intermediate state, per-task step limits, and a daily run limit.
- Original, editable role playbooks for strategy, research, product engineering, analytics, SEO, support, outreach, paid acquisition, and finance.
- Owner-controlled manual, supervised, and autonomous modes. Scheduled autonomous work can run without an open browser.
- Exact approval for every external write. The owner sees the recipient, message, branch, file content, or issue before execution.
- Persistent tool receipts. Duplicate call IDs reuse a completed result. Interrupted or ambiguous calls fail for inspection, never silently retry.
- Saved Markdown/text/code artifacts, shared memory, a task activity trail, and consistent SQLite backup tooling.
- Optional GitHub, Brave Search, HTTPS website reading, and Resend adapters. Missing credentials remove tools from the model's available tool list.
- Private owner login, expiring HttpOnly sessions, CSRF and origin checks, login throttling, request size limits, host checks, and a strict content security policy.
- Docker deployment files, a separate supervised worker, health probes, automated tests, and GitHub CI.

## Capability boundaries

| Area | Included now | Not implemented in v0.1 |
|---|---|---|
| Product work | Read repository files and issues; create branches, commits, issues, and draft PRs with approval | Shell execution, running tests in customer repositories, autonomous merge/deploy |
| Research and SEO | Brave search, allowlisted public pages, source-based reports and content drafts | Search Console, rank tracking, backlink databases, automatic CMS publishing |
| Analytics and finance | Analysis of supplied evidence, original playbooks | PostHog, GA4, Stripe, database access, session replay |
| Support and sales | Supplied-ticket analysis, exact approved Resend email sends | Inbound inbox sync, CRM, bulk sequences, LinkedIn |
| Paid growth and creative | Campaign plans, copy and creative briefs | Ad activation, image/video generation, real budget management |
| Agent operations | Nine role prompts, durable tasks, schedules, memory, approvals, audit events | Parallel subagents, browser automation, plugins/MCP, multi-tenant SaaS, SSO |

Evidence for the included paths: [API checks](tests/test_app.py), [engine checks](tests/test_engine.py),
[tool checks](tests/test_tools.py), and [provider contract checks](tests/test_provider.py).
[Release acceptance checks](tests/test_release_acceptance.py) restore a real SQLite backup into a separate workspace.
They verify saved approvals, artifact retrieval, and refusal to replay interrupted work.
Model responses in these tests are scripted. No capability has live provider or production acceptance evidence yet.
The missing capabilities above remain planned, not available integrations.

Roles describe how an agent works. They do not manufacture access to a service. Do not grant this worker an unrestricted shell or mount a Docker socket to fill those gaps.

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

Open `http://localhost:8000`. You can save drafts, memory, and schedules without provider keys. Starting an AI task requires an OpenAI key. There is no mock success mode in the application. Tests use explicit fake providers and cannot send email or spend money.

Configure optional services using `.env.example`. Restart **both** processes after changing server configuration. Secrets never belong in task instructions, memory, screenshots, or git. Rotate the session secret to invalidate all existing sessions.

## First real task

1. Add your product description in Memory under `product`.
2. Set your goal in Settings. Keep supervised mode while learning the workflow.
3. Connect OpenAI using the server environment. A configured flag does not prove the key is valid.
4. Create a small task, such as: “Use my product note to draft three positioning options. Save a Markdown report.”
5. Read the result and artifact. For connected actions, inspect the exact values in Decisions before approving.
6. Set provider-side spending limits before enabling recurring autonomous runs.

OpenAI calls use [the Responses API with function tools](https://developers.openai.com/api/docs/guides/function-calling). Set `A4G_MODEL` to a Responses-compatible model available to your account. Model/API changes may require adapter updates.

## How autonomy works

```text
Owner task or schedule → durable queue → single worker → model proposes tools
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
