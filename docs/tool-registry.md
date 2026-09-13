# Executable tool registry

All 12 implemented tools now use `ToolRegistry.execute(name, args, task_id=..., call_id=...)`.
The engine creates approval requests. The registry checks approval again before any new execution.
Callers cannot supply credentials, choose provider endpoints, or skip execution controls through this public interface.

## Discovery and availability

The owner can inspect `GET /api/tools` after login. This endpoint does not execute tools.
Each executable entry declares its description, category, input and output schemas, authentication fields, scopes, permission rule, and risk.
It also declares rate limits, timeout limits, and required audit records.
Only configured implementations appear in the model's function list. Catalog entries never become model functions by themselves.

| State | Meaning |
|---|---|
| planned | No implementation exists for this category. |
| unavailable | An implementation lacks required server configuration or workspace storage. |
| configured | Required server values exist. Provider acceptance is not established. |
| verified | A validated completed receipt exists in this workspace's history. This is historical evidence, not a current health guarantee. |

Credential checks run on the server and return field names, never values.
They check configuration presence, not token validity or provider permissions. A successful real invocation supplies that evidence.
A credential change can invalidate earlier acceptance. Inspect receipt dates and run another bounded read after changing configuration.
Verification is scoped to the workspace database. Test databases cannot verify the running owner's deployment.

The catalog covers Browser, Shell, Filesystem, Git, GitHub, Web Search, Email, Calendar, Database, Analytics, Cloud, Media, and Social/API.
Filesystem means saved text artifacts only. Database means workspace notes only.
GitHub is a scoped remote adapter. It does not imply a local Git or shell implementation.
Unimplemented categories remain planned. Adding them requires their own implementation and isolation work.

## Execution boundary

JSON Schema validation rejects invalid arguments before invocation and invalid results before completing receipts.
Schema errors omit rejected values. Schema discovery returns copies, so callers cannot edit schemas through discovery responses.

One SQLite transaction checks the active task, exact approval, previous receipt, and shared tool rate counter.
The transaction records intent before releasing the database for adapter work.
Mutating tools always need approval. Manual mode also requires approval for reads and artifact creation.
An approval binds task ID, call ID, tool name, and exact arguments.

A completed matching receipt returns its validated saved result without another invocation or rate charge.
Incomplete or mismatched receipts refuse execution. A bad output can follow a successful external write.
These failures stay incomplete and require inspection. They never trigger automatic external retries.
Rate counters use the existing database table, so separate registry instances share them across restarts.
Each tool allows 60 new attempts per 60-second window. Failed attempts consume the reserved allowance.

Execution logs intent, completion, verification, refusal, and failure without raw provider errors.
Arguments remain in the existing private receipt store. Do not put secrets in arguments or task prompts.
The registry validates and redacts results before storage. Results above the context limit fail instead of returning truncated data.

## Time and isolation limits

Each invocation has a 60-second cooperative deadline, checked before dispatch, between network chunks, and after execution.
HTTP requests use bounded connect and read timeouts, restricted destinations, and bounded response buffers.
DNS resolution and an active blocking operation can outlast the cooperative deadline.
A timed-out external write may still have happened. Inspect the incomplete receipt before taking further action.

This registry is trusted server code, not a sandbox for arbitrary Python extensions.
Underscored adapter methods are implementation details, not supported invocation interfaces.
Future untrusted workers need process isolation and hard termination, as tracked by the sandbox and worker tasks.
The registry does not add distributed execution or change existing single-host support.

## Validation and live evidence

Run `uv run pytest -q` and `uv run ruff check .` before delivery.
Registry tests cover every tool's invalid input and output, exact approval, manual mode, cancellation, shared rates, and concurrent receipts.
They also cover failed-output audit records, timeout refusal, and authenticated catalog discovery.
Existing backup restoration tests continue to exercise the complete engine path.

Run `uv run python scripts/check_registry.py` for a bounded real HTTPS read.
It reads `https://example.com/`, validates the text result, and checks receipt reuse.
It uses a temporary tagged workspace, no provider credentials, no model call, and no external write.
The temporary database and test records are removed on exit.

September 13, 2026: this live read passed and returned 142 text characters through one completed receipt.
The public raw GitHub documentation probe returned HTTP 404. It did not establish GitHub adapter acceptance.
GitHub writes, Brave Search, and Resend have simulated regression evidence only in this release.
No email was sent, and no production deployment or live model completion is claimed.
