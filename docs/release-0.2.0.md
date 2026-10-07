# agentforge 0.2.0: self-hosted AI operator release

Published September 27, 2026. Release tagged September 25, 2026.

agentforge 0.2.0 is the first tagged public release of our self-hosted AI operator.
It keeps tasks, conversations, plans, approvals, and saved results in one workspace you host.
The project was previously called Agent4Good.

**[Install agentforge 0.2.0](https://github.com/iamaanahmad/agentforge/blob/v0.2.0/README.md#run-locally)**

## What ships

- Separate task conversations with follow-ups and saved results.
- Missions with success criteria, dependent tasks, and evidence review.
- Nine specialist roles, scoped action controls, and an execution timeline.
- Saved checkpoints and receipts for recovery. Uncertain writes stop for inspection.
- Model routing for OpenAI, Anthropic, AWS Bedrock, and Google Vertex AI.
- Settings for your installation's name, tagline, logo, and accent color.

Each installation supports one owner. SQLite runs on one host.
Optional PostgreSQL, coding sandbox, and browser services require separate setup.
Read the [agentforge 0.2.0 GitHub release](https://github.com/iamaanahmad/agentforge/releases/tag/v0.2.0) for the shipped scope and downloadable files.

## MIT license and setup

Project-owned code is available under the [MIT license](https://github.com/iamaanahmad/agentforge/blob/v0.2.0/LICENSE).
Dependencies retain their own licenses. Third-party binary and container redistribution review remains open.

The setup guide covers installation, model credentials, the web app, and the worker.
Follow the [self-host guide](self-host-guide.md) from a fresh install to a saved result.
You need Git, Python 3.12 or 3.13, uv, and a model-provider account for AI work.
Provider charges are separate. You manage hosting, secrets, and backups.

The Python package remains `agent4good`, the command remains `a4g`, and settings retain the `A4G_` prefix.
Existing owners should back up their data and vault keys before updating.

## What we have verified

On September 27, a fresh installation of v0.2.0 completed one supervised mission using live AWS Bedrock calls.
The mission produced a positioning brief. Its first draft invented claims and failed review.
The test operator supplied a correction, and the model saved a revised brief that met both acceptance criteria.
Both drafts remained retrievable after a service restart and a new login.

The [agentforge fresh-install mission report](https://github.com/iamaanahmad/agentforge/blob/main/docs/fresh-mission-acceptance.md) includes the failed draft, correction, saved results, and repeat procedure.
This later test adds evidence beyond the original release notes.

One assisted text mission does not establish unattended quality, repeated reliability, or production readiness.
Browser and coding missions, customer demand, and production operation remain unverified by this test.

Start with one small task, inspect its result, and confirm you can retrieve it again.
