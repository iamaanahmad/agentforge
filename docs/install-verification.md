# Clean installation evidence

On September 25, 2026, a fresh HTTPS clone of the open-source preparation branch installed with an empty uv package cache. The test generated new private configuration and used no existing provider credentials.

| Step | Result | Elapsed |
|---|---|---|
| Fresh branch clone | Passed | 0.73 seconds |
| `uv sync --frozen --group dev` | Passed | 1.96 seconds |
| `uv run python scripts/bootstrap.py` | Passed; configuration mode 0600 | 0.16 seconds |
| Real HTTP server, health, owner login, draft save, retrieval, cancellation | Passed | 3.13 seconds |
| API, branding, backup and saved-result restoration tests | 28 passed | 8.67 seconds |

Total: 14.65 seconds on the test host. Python and uv were already installed. This is not an estimate for every machine.

The saved-result tests use scripted providers. No live AI call occurred in this clean installation, and this does not prove provider setup or production hosting. Configure your own provider and complete the README's first real task before relying on unattended execution.

The temporary server, clone, configuration, databases and package cache were removed. The existing workspace was not used for this test.

Separately, the full local suite passed 449 tests with 37 service-dependent skips. Real browser, sandbox and distributed-service checks run in GitHub CI. Release readiness requires those checks to pass on the final revision.
