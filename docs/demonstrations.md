# Autonomous workflow demonstrations

The three requested complete demonstrations are not yet accepted.
Component checks are useful preparation, but do not prove one composed mission.

## Repeatable prerequisite checks

Run from a development checkout with `uv sync --frozen --group dev` completed:

```sh
uv run python scripts/demonstration_readiness.py --output /tmp/a4g-demo-evidence-new
```

The output directory must not exist. The command never overwrites previous evidence.
Exit 1 means a failed test, incomplete collection, timeout, or malformed evidence.
Exit 2 means the command finished, but complete workflow acceptance remains open.
There is no success exit code for the complete demonstration contract.

The command writes one JUnit file and log per group, plus a combined report.
It retains isolated test databases and artifacts under each group's fixture directory.
Quality traces preserve critic decisions, revisions, receipts, and events.
The report hashes its top-level evidence files. No live model costs are inferred.
Keep evidence private. Fixture credentials are synthetic and must never serve a real deployment.
Tests stop their controlled services; retained fixture files are evidence, not active customer records.
After reviewing or copying evidence, the owner can remove that run's private directory.

No production data directory or model credential enters these tests.
The command strips application configuration and provider tokens from the subprocess environment.
It forwards only the two optional pinned container image settings.
GitHub source and deployment responses remain controlled fixtures, even in container runs.

## Real execution backends

Use a Docker host to build both reviewed images:

```sh
docker build -f sandbox/Dockerfile -t agent4good-sandbox:demo .
docker build -f browser/Dockerfile -t agent4good-browser:demo .
export A4G_TEST_SANDBOX_IMAGE="$(docker image inspect agent4good-sandbox:demo --format '{{.Id}}')"
export A4G_TEST_BROWSER_IMAGE="$(docker image inspect agent4good-browser:demo --format '{{.Id}}')"
uv run python scripts/demonstration_readiness.py --output /tmp/a4g-demo-real-new
```

Missing images cause explicit blocked results. They never count as real-backend passes.
Inspect retained browser screenshots before describing visual acceptance.
Check that no containers remain with `agent4good.browser=true` or `agent4good.sandbox=true` labels.
The existing browser and sandbox CI workflows also run these real-backend component checks.

## Development journey still required

Create a tagged mission with a research source and a controlled repository.
Record the planner's dependent tasks and the research evidence.
Run actual code changes and tests through the sandbox broker.
Force one observable defect and retain the isolated critic's rejection.
Revise the same candidate and pass separate verification.
Hold the exact deployment action at the owner approval gate.
Before approval, verify that the controlled target has not changed.
After explicit approval, deploy and read back the target's content and actual receipt.
Retain the same mission's task IDs, source revision, artifacts, events, and costs.
Retire the controlled deployment after inspection.

Current prerequisite checks cover mission planning, real sandbox execution, and critic revision separately.
Merge and workflow checks use simulated GitHub responses. They are not deployment receipts.
Connecting these stages into one mission and proving a real approved deployment remain unfinished.

## Browser journey still required

Use a marked test identity on the controlled form and file fixture.
Record approved writes, transfer files, interrupt a page, and recover the scoped session.
Read back saved data and inspect the screenshot, file hashes, and timeline.
Prove that an uncertain submission does not repeat. Clean up the fixture and session.
Current component checks cover these broker behaviors with real Chromium when configured.
One model-driven browser mission remains unverified.

## Multi-worker restart journey still required

Create a tagged mission that delegates work and performs one approved controlled external action.
Kill its worker process after the receiver accepts the action.
Restart with the same durable state and finish the remaining work.
Compare the receiver's independent ledger with the task receipts. The accepted action must appear once.
Retain task trees, process exit evidence, and the final timeline.

Current checks prove mission restart, parallel worker restart, and HTTP receipt recovery separately.
Their models are scripted. A single composed multi-worker external-action mission remains unfinished.

## Live model acceptance

Provide a model credential through secure server configuration or the encrypted vault.
Never paste keys into chat, commands, evidence reports, or source control.
Use the configured fixed provider endpoints and bounded task budgets.
Record actual token usage; keep cost null when no reliable estimate exists.
Label fault injection as controlled even when the surrounding model calls are live.
A skipped, blocked, or scripted run must never become a live acceptance claim.

## September 27 readiness

The configured Bedrock model is available. Existing live text mission evidence is
in [fresh mission acceptance](fresh-mission-acceptance.md).
Only the strategist role has a model credential grant in the inspected installation.
Route availability does not establish permission for every worker role.
Do not request another provider key or broaden grants to bypass this boundary.
All roles share tool policy, so strategist-only test workers remain an option.

Local browser and sandbox sockets are unset. Docker is absent on that host.
GitHub can run the real container prerequisites without receiving model credentials.
The `Demonstration prerequisites` workflow builds both images and records all three groups.
It requires every prerequisite to pass, but never certifies complete demonstrations.
Its public artifacts contain reports, logs, JUnit results, and the synthetic browser screenshot.
Private fixture databases and credential stores are excluded.

To finish the composed live runs, use a controlled Docker-capable execution host
with scoped Bedrock access and both private brokers. Keep provider secrets off CI.
Production hosting remains a separate delivery dependency.
Prepare the exact test deployment and its expected content before asking for approval.
No approved deployment destination or allowlisted deployment workflow is configured locally.
