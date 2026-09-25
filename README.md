# agentforge

[![Verify](https://github.com/iamaanahmad/agentforge/actions/workflows/ci.yml/badge.svg)](https://github.com/iamaanahmad/agentforge/actions/workflows/ci.yml)
[![Release evidence](https://github.com/iamaanahmad/agentforge/actions/workflows/release.yml/badge.svg)](https://github.com/iamaanahmad/agentforge/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%20%7C%203.13-blue.svg)](pyproject.toml)
[![Latest release](https://img.shields.io/github/v/release/iamaanahmad/agentforge)](https://github.com/iamaanahmad/agentforge/releases/latest)

**A self-hosted AI operator with saved work and human control.**

Give agentforge a task, choose a specialist, and follow its work from request to saved result.
Keep conversations, plans, approvals, and evidence in one workspace you host.

[Quick start](#run-locally) · [Deployment](#deploy-and-operate) · [Documentation](#documentation) · [Contributing](CONTRIBUTING.md) · [Releases](https://github.com/iamaanahmad/agentforge/releases)

## What you can do

- **Keep each task in its own conversation.** Send follow-ups, answer questions, and retrieve previous replies and results.
- **Turn objectives into missions.** Define success criteria and limits, then review dependent tasks and their evidence.
- **Work with nine specialists.** Use strategy, research, engineering, analytics, SEO, support, outreach, ads, and finance roles.
- **Inspect execution.** Follow plans, child workers, approvals, retries, usage, and saved artifacts in the timeline.
- **Control external actions.** Review exact write requests before execution. Scope permissions and bound task work.
- **Resume durable work.** Saved checkpoints and receipts support recovery. Uncertain writes stop for inspection.
- **Choose your models.** Configure OpenAI, Anthropic, AWS Bedrock, or Google Vertex AI across six task routes.
- **Customize your installation.** Set a name, tagline, logo, and accent color without changing package names or stored data.

agentforge supports one owner per installation. SQLite runs on one host.
An optional PostgreSQL stack separates control services from worker replicas.
This is an early release with automated integration checks, not a claim of verified production operation.

Previously Agent4Good: Python imports remain `agent4good`, the CLI remains `a4g`, and settings retain the `A4G_` prefix.

## Run locally

You need Git, Python 3.12 or 3.13, and [uv](https://docs.astral.sh/uv/).
A model-provider account is required to run AI tasks. Provider charges are separate.
Node is needed only for contributor JavaScript checks.

### 1. Install and create your configuration

```sh
git clone https://github.com/iamaanahmad/agentforge.git
cd agentforge
uv sync --frozen --group dev
uv run python scripts/bootstrap.py
```

Read the generated `.env` file locally for your workspace password.
Keep this file private. Never put credentials in task messages, memory, screenshots, or Git.

### 2. Connect a model

For the default OpenAI route, set `A4G_OPENAI_API_KEY` in `.env`.
Choose an available Responses-compatible model with `A4G_MODEL`.

| Provider | Setup instructions |
|---|---|
| OpenAI Responses | [Model routing and environment settings](docs/model-routing.md) |
| Anthropic Messages | [Model routing and environment settings](docs/model-routing.md) |
| AWS Bedrock Converse | [Cloud credentials, models, and roles](docs/cloud-models-and-skills.md) |
| Google Vertex AI Gemini | [Cloud credentials, models, and roles](docs/cloud-models-and-skills.md) |

Routes pin their selected profile for each task. Failed calls do not silently switch providers.
Use provider-side spending limits before starting recurring work.

### 3. Start the web app

```sh
uv run uvicorn agent4good.app:create_app --factory --host 127.0.0.1 --port 8000
```

### 4. Start the worker in another terminal

From the same repository directory:

```sh
uv run python -m agent4good.worker
```

Open **http://localhost:8000** and sign in with your generated password.
Keep both processes running. Restart both after changing server configuration.

You can save drafts and memory without model credentials. AI execution requires a valid selected provider.
There is no application mode that reports fake model success.
See [clean-install evidence](docs/install-verification.md) for tested steps and remaining limits.

## Complete your first task

1. Add a short product description in Memory under `product`.
2. Set your goal in Settings. Start in supervised mode.
3. Confirm the selected route and credential access in Connections.
4. Create a small task: “Use my product note to draft three positioning options. Save a Markdown report.”
5. Follow progress in the task conversation and timeline.
6. Open the result and saved artifact. Reopen the task to confirm the result remains available.
7. Review exact requests in Decisions before allowing connected writes.

Start with a bounded task before asking for a full mission.
A configured credential indicator does not prove account access or model quality.

## How control works

```text
Task or schedule → durable queue → worker → model proposes an action
                                              ↓
                                  schema and permission checks
                                     ↓                  ↓
                                allowed read      exact write approval
                                     └────────┬─────────┘
                                       saved receipt
                                              ↓
                                   result and saved artifacts
```

| Mode | Starting work | Tool decisions |
|---|---|---|
| Manual | You start tasks; schedules create drafts | You approve every tool call |
| Supervised | You start tasks; schedules create drafts | Reads and artifacts run; external writes and memory changes need approval |
| Autonomous | You start tasks or schedules enqueue them | External write approvals remain mandatory |

Approvals bind the task, tool, call, and exact arguments. Rejection stops the task.
Cancellation prevents future steps; an external request already in progress can still finish.
Step and daily run limits bound work. They are not provider billing limits.

## Deploy and operate

Read [deployment and recovery](docs/deployment.md) before exposing the app to the internet.
The Compose stack runs separate non-root web and worker services, with persistent storage and optional Caddy HTTPS.

```sh
# First configure your domain, secrets, and production settings in .env.
docker compose --profile tls up --build -d --wait
```

Before using a deployment for real work:

- Set the public HTTPS origin, allowed hosts, secure cookies, and strong owner/session secrets.
- Configure model credentials and role access. Keep vault keys outside the data volume.
- Check web health and the worker heartbeat.
- Run a small live task, review its output, and retrieve its saved result.
- Create a backup and prove restoration into a separate destination.
- Set provider budgets and review permissions for each connected adapter.

Use [PostgreSQL deployment](docs/distributed-infrastructure.md) when you need separate worker replicas.
Use [credential security](docs/credential-security.md) for vault migration, rotation, and access boundaries.
Automated tests do not replace these checks on your own host.

## Integrations and boundaries

| Area | Available | Boundary |
|---|---|---|
| Model execution | OpenAI, Anthropic, Bedrock, Vertex AI | Your accounts, permissions, and model access are required |
| Research | Brave Search, allowlisted HTTPS reading, approved DataForSEO keyword queries | No Search Console or automatic CMS publishing |
| GitHub | Scoped reads, branches, files, issues, draft PRs, checked merges, configured workflows | Exact approvals and repository restrictions apply |
| Email | Exact approved Resend sends | No inbound inbox sync, CRM, or bulk sequences |
| Coding | Optional isolated offline sandbox broker | No unrestricted host shell or network package installation |
| Browser | Optional isolated Chromium broker with approved journeys | No challenge bypass or cross-task session sharing |
| Analytics and finance | Analysis of supplied evidence | No built-in PostHog, GA4, Stripe, or customer-database connector |
| Paid growth | Plans, copy, and creative briefs | No ad activation, spending control, or media generation |

Roles guide work; they do not create service access. Missing credentials remove unavailable tools from the model's tool list.
Optional browser and coding brokers require their own setup. Never grant an unrestricted shell or Docker socket as a shortcut.

## Documentation

| Guide | What it covers |
|---|---|
| [Architecture](docs/architecture.md) | Processes, storage, extension points |
| [Task conversations](docs/task-conversations.md) | Separate chats, replies, questions, and follow-ups |
| [Missions](docs/missions.md) | Objectives, dependent plans, criteria, and evidence |
| [Specialist workers](docs/specialist-workers.md) | Parallel delegation, messages, and cancellation |
| [Scheduling](docs/scheduling.md) | Time, event, condition, dependency, and priority rules |
| [Execution timeline](docs/execution-timeline.md) | Plans, events, approvals, usage, and export |
| [Durable execution](docs/durable-execution.md) | Checkpoints, retries, receipts, and recovery limits |
| [Tool registry](docs/tool-registry.md) | Typed contracts and validated execution |
| [Action policies](docs/action-policies.md) | Scoped permissions and work limits |
| [Layered memory](docs/layered-memory.md) | Retrieval, corrections, isolation, and quarantine |
| [Independent quality](docs/independent-quality.md) | Critics, verifiers, and bounded revisions |
| [Outcome learning](docs/outcome-learning.md) | Evidence-linked history and later planning |
| [Developer tools](docs/developer-tools.md) | Authenticated API, CLI, SDK, and recorded-only replay |
| [Browser control](docs/browser-control.md) | Broker setup, supported actions, and safety limits |
| [Isolated coding](docs/isolated-coding.md) | Sandbox resources, delivery, and threat model |
| [Instance branding](docs/white-label.md) | Name, tagline, logo, and color settings |
| [Separate installations](docs/projects.md) | Independent storage and configuration for different products |

“Projects” in the installation guide means separate deployments for different products.
There is no project switcher or separate workspace feature inside one installation.

## Contribute

Bug reports, documentation improvements, and focused code changes are welcome.
Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup, review expectations, and compatibility rules.
Use [issues](https://github.com/iamaanahmad/agentforge/issues) for reproducible bugs and feature proposals.
Follow [SECURITY.md](SECURITY.md) for vulnerabilities; do not post secrets or private data in public issues.

```sh
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
node --check agent4good/static/app.js
node --check agent4good/static/chats.js
uv build
```

CI checks Python 3.12 and 3.13, package installation, containers, and release evidence.
The release gate requires regression, infrastructure, browser, sandbox, and failure suites.
Local service skips are not passing integration evidence. Most model tests use controlled responses.

## License

Project-owned code is released under the [MIT license](LICENSE).
Dependencies retain their own terms. Read the [dependency inventory](docs/dependency-licenses.md) before redistributing dependencies or containers.
