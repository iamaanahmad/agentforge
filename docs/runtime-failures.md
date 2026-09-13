# Runtime failure contract

The release candidate requires regression and real controlled integration evidence for the same source revision.
This is not a production certification. Scripted model output does not prove live provider completion.

## Failure matrix

Run `uv run pytest` for local coverage. Docker and PostgreSQL cases run in their dedicated CI services.
Each row names an executable test and its safe expected result. Existing capability tests remain owned by their modules.

| Failure | Executable test | Expected safe result | Evidence class |
|---|---|---|---|
| Restart recovery | `test_runtime_failures.py::test_process_crash_compares_external_effect_and_durable_receipt` | A new process resumes a completed receipt without another request. | Real process, HTTP receiver and SQLite; scripted model |
| Duplicate external writes | Same test, `after_receipt` parameter | Replanning with a new call ID leaves exactly one external ledger row. | Real controlled side effect |
| Tool failure | `test_durable_execution.py::test_read_failure_replans_and_finishes_after_restart` | The later plan sees the failure and saves an explicit limitation. | Injected adapter failure |
| Browser failure | `test_browser.py::test_real_timeout_and_unapproved_form_do_not_repeat` | A timed-out or unapproved form never repeats. | Real Docker Chromium and controlled server |
| Model failure | `test_durable_execution.py::test_model_transport_retries_are_bounded` | The task fails after its retry budget. | Injected model timeout |
| Network timeout | `test_durable_execution.py::test_read_transport_retry_limit` | Read retries stop at the configured limit. | Injected transport timeout |
| Partial completion | `test_runtime_failures.py::test_process_crash_compares_external_effect_and_durable_receipt[after_acceptance]` | One external effect remains; an uncertain local receipt holds the task. | Real process death and HTTP side effect |
| Conflicting workers | `test_workers.py::test_external_write_conflict_is_held` | A conflicting write remains held. | Controlled adapter |
| Approval tampering | `test_policy.py::test_approval_tampering_and_stale_scope_never_execute` | Changed approvals cannot dispatch tools. | Local policy and database |
| Unauthorized tools | `test_engine.py::test_unapproved_unknown_tool_fails_closed` | Unknown tools fail without dispatch. | Scripted model |
| Sandbox escape attempts | `test_sandbox.py::test_real_host_secret_filesystem_network_and_privileges` | Code cannot access host secrets, network, or elevated privileges. | Real Docker sandbox |
| Scheduler duplication | `test_scheduling.py::test_separate_scheduler_processes` | Competing schedulers create one occurrence. | Real processes and SQLite |
| Concurrent workers | `test_infrastructure.py::test_real_worker_process_kill_recovery_and_drain` | Leases fence workers; restart preserves completed receipts. | Real PostgreSQL, processes; scripted model |
| Memory corruption | `test_memory.py::test_corruption_quarantines_then_explicit_correction` | Corrupt memory stays quarantined until explicit correction. | Real local database |
| Crash before request | `test_runtime_failures.py::test_process_crash_compares_external_effect_and_durable_receipt[before_request]` | No external effect occurs; the uncertain intent stays held. | Real process death and HTTP receiver |

## Crash evidence

The receiver binds only to loopback and writes each request into a separate SQLite ledger.
It deliberately does not deduplicate requests. Duplicate runtime dispatch therefore fails the test.
The worker uses the real HTTP client, approval path, adapter, journal, and receipt code.
Only the fixed provider destination changes to the controlled receiver. No customer receives a message.
A spawned worker exits immediately before the request, after acceptance, or after the local receipt.
The parent compares receipt state, task state, request count, payload, and action identity after recovery.
Completed receipts survive replanning. Uncertain writes require inspection, including intents that never reached the receiver.
This conservative hold does not claim exactly-once delivery or provider receipt reconciliation.

## Release enforcement

`.github/workflows/release.yml` calls all four existing verification workflows for the same revision.
Its `release-required` job uses `always()` and requires every dependency to succeed.
Failed, cancelled, or skipped jobs cannot produce the `tested-release-candidate` package artifact.
Browser, sandbox, infrastructure, and failure suites also reject skipped, empty, malformed, and missing JUnit evidence.
Required sentinel tests prevent an absent real integration test from silently becoming a passing report.
The normal broad regression run can skip services unavailable on that runner. Dedicated service jobs cannot.
JUnit reports upload even on failure. GitHub stores them as workflow artifacts under its retention policy.
Gate rejection tests exercise skipped, failed, empty, absent, malformed, and missing required results.

For local reports, use `uv run python scripts/check_test_evidence.py REPORT.xml --require TEST_NAME`.
The command prints separate passed, failed, skipped, and missing counts and exits nonzero on incomplete evidence.

Repository administrators should require `release-required` on protected branches and use this workflow for candidate packages.
The connected GitHub token cannot inspect branch protection (HTTP 403); no branch protection change is claimed.
This gate controls the candidate artifact. It cannot prevent administrators from bypassing CI or publishing elsewhere.

## Evidence boundaries

- Passed: only assertions from a completed test or successful workflow on the reported revision.
- Failed: assertions, missing reports, missing tests, or required jobs that did not succeed.
- Skipped: unavailable local Docker/PostgreSQL cases. These must pass in dedicated CI before candidate packaging.
- Credential-blocked: live OpenAI/Anthropic and customer adapter acceptance without configured test credentials.
- Unverified: production hosting, production throughput, and useful live model completion.

No release check converts absent live credentials into success. No live provider check runs in this controlled workflow.
A production claim still requires separate live demonstrations and deployment evidence.
