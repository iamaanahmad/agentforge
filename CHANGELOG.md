# Changelog

## 0.2.0 - 2026-09-25

First tagged public release of agentforge, formerly Agent4Good.

### Included

- MIT license for project-owned code, included in Python distribution metadata and archives.
- A new README with status badges, setup steps, provider guides, operations checks, and contribution links.
- Saved task conversations with follow-ups, owner questions, and retrievable results.
- Missions, nine specialist roles, durable execution, scoped approvals, and an execution timeline.
- OpenAI, Anthropic, AWS Bedrock, and Google Vertex AI adapters with configurable routing.
- Instance branding and contributor guides, with separate-installation instructions.
- Optional PostgreSQL workers, offline coding sandboxes, and isolated browser brokers.

### Compatibility

The Python package remains `agent4good`; the CLI remains `a4g`.
Existing `A4G_` settings and stored identifiers retain their names.
This release does not add multiple workspaces or users inside one installation.

### Verification and limits

The release gate requires regression, container infrastructure, browser, sandbox, and failure checks.
Controlled model responses validate runtime contracts, not arbitrary model output quality.
Production hosting and complete live-model missions remain unverified.

Python release assets contain this project's code. They do not bundle dependency wheels or container images.
Dependencies retain their own licenses. Redistribution of third-party binaries needs a separate review.
See [dependency licenses](docs/dependency-licenses.md) and [deployment](docs/deployment.md).
