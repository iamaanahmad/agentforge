# Self-host agentforge: install to saved result

This guide takes one owner from a fresh local install to a saved AI result.
Its mission steps draw on the agentforge 0.2.0 [fresh-install mission report](fresh-mission-acceptance.md).
You run the app and worker on your own machine. This is a local evaluation, not a production deployment.

## What you need

- Git, Python 3.12 or 3.13, and [uv](https://docs.astral.sh/uv/).
- A model-provider account with access to a supported tool-capable model. Calls may incur charges.
- Two terminals for the web app and worker.

One installation supports one owner. You do not need the optional browser or coding brokers for a text result.
Keep all credentials outside task prompts and shared memory. Start with supervised mode and no external writes.

## 1. Install and sign in

```sh
git clone https://github.com/iamaanahmad/agentforge.git
cd agentforge
uv sync --frozen --group dev
uv run python scripts/bootstrap.py
```

Bootstrap writes a private `.env` with a generated owner password and session settings.
Read the password locally. Keep `.env` out of Git and screenshots.

For the default OpenAI route, add your own `A4G_OPENAI_API_KEY` to `.env`.
Set `A4G_MODEL` to a Responses-compatible model your account can use.
For Anthropic, Bedrock, or Vertex, follow [model routing](model-routing.md) and [cloud setup](cloud-models-and-skills.md).
Bedrock and Vertex credentials require the encrypted vault and role grants.
Restart both processes after changing model settings.

Start the web app in the first terminal:

```sh
uv run uvicorn agent4good.app:create_app --factory --host 127.0.0.1 --port 8000
```

Start the worker in the second terminal, from the same directory:

```sh
uv run python -m agent4good.worker
```

Open `http://localhost:8000` and sign in with the generated password.
Check that the worker is online. Open Connections and confirm the selected model route is available.
An available route does not prove that a provider call will succeed.

## 2. Save one bounded result

1. Add a short, factual product note under Memory's `product` section.
2. Set your goal in Settings and keep supervised mode.
3. Create a task for the strategist: "Use my product note to draft three positioning options. Save a Markdown report. State what remains unknown."
4. Select a route available to the strategist. Start the task and watch its conversation and timeline.
5. Open the final result and its saved artifact. Read the full report and check every factual claim.
6. Reopen the task to confirm the saved result remains available.

An unavailable route blocks task start. A configured credential can still fail on permissions, billing, or model access.
If the worker is offline, the task can remain queued. Check both processes before retrying.

## 3. Try a supervised mission

Open Missions and create a draft with one narrow objective, such as a positioning brief.
Add clear success criteria that you will review yourself. Set a small task and model budget.
Allow only the strategist and `artifact_write`; leave external writes disabled.
Save the draft, then start it. The planning model creates a task plan for you to inspect.

Open the resulting artifact and compare it with the source note and criteria.
Reject unsupported claims. If needed, add an owner-directed correction and run the dependent step.
Accept each owner-review criterion only after you have inspected the evidence.
Use **Check completion** to evaluate the mission. Reopen the mission and download the accepted artifact.
The [mission guide](missions.md) explains criteria, budgets, correction, and recovery controls.

The [recorded Bedrock run](fresh-mission-acceptance.md) completed this path after the owner rejected its first draft.
Both owner criteria passed after a correction. That single assisted run does not prove unattended quality or repeat reliability.

## If you deploy on your own host

Use [deployment and recovery](deployment.md) for the one-host SQLite stack, HTTPS, secrets, backups, and health checks.
Do not expose the local development port to the internet.
The documented HTTPS path uses Docker Compose and needs a domain, persistent storage, and fresh production secrets.
Verify login, worker health, one live artifact, and backup restoration on your host before relying on it.
No agentforge production host has been verified by this project.

For an exact repeat of the tested Bedrock mission, including its input record and correction, use the [acceptance procedure](fresh-mission-acceptance.md#repeat-the-acceptance-procedure).
Its model output can differ on a new run.
