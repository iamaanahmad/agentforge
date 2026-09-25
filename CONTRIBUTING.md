# Contributing to agentforge

agentforge was previously called Agent4Good. The Python package and `A4G_` settings keep their existing names for compatibility.

The project uses the [MIT license](LICENSE). Submit only work you have the right to contribute.
Keep existing copyright and third-party notices.

## Start with a reproducible issue

Describe the problem, expected behavior, actual behavior, and the smallest safe reproduction.
Include the revision, Python version, operating system, and SQLite or PostgreSQL mode.
Never attach `.env`, credentials, database copies, private conversations, or unredacted logs.
See [SECURITY.md](SECURITY.md) for vulnerability handling.

## Local checks

Use Python 3.12 or 3.13 and uv. Follow the [setup guide](README.md#run-locally).
Run these checks before submitting a pull request:

```sh
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
node --check agent4good/static/app.js
node --check agent4good/static/chats.js
uv build
```

Node checks browser JavaScript; the application does not require Node at runtime.
Container suites need Docker and their documented services.
Skipped container tests are not passing integration evidence.
The release workflow runs those service checks separately.

## Keep changes reviewable

Use a branch from current `main`. Keep each pull request focused on one outcome.
Explain the changed behavior, tests, compatibility impact, and any remaining limits.
For interface changes, include screenshots without private data.
Add regression tests for behavior changes and document new settings.
Never weaken authentication, approval, isolation, or recovery tests to make a change pass.

Keep `agent4good`, `a4g`, `A4G_`, database filenames, and stored identifiers compatible.
A visible brand change must not silently migrate data or break existing installs.
Do not add provider calls, external writes, or paid services to normal tests.
Use controlled providers and marked test data.

## Review

Maintainers review code and CI evidence before merge. Passing CI does not prove production safety or live-model quality.
Discuss large changes before implementation. Be respectful and focus review comments on the work.
