# Missions

A mission preserves one owner objective and its original success criteria. Its plan becomes durable tasks, executed by the same real worker runtime as ordinary tasks. Open **Missions** to save an objective, review criteria, deadline, budget and permissions. Saving creates a draft. Starting invokes a bounded planner using the configured planning model.

## Contract and execution

The authenticated `/api/missions` API accepts `title`, `objective`, `criteria`, `constraints`, `budget`, `deadline`, `tools`, `agents`, `writes`, and `priority`. The stored contract is immutable. A different objective requires a new mission. Deadline timestamps require a timezone. The UI converts your local date to UTC.

Each criterion has a unique `id`, `description`, and one `kind`:

- `owner`: the authenticated owner records evidence and explicitly accepts or rejects the criterion. Models cannot write these reviews.
- `artifact`: an exact `name` and required `contains` text must match a saved artifact with a successful write receipt. Private-object storage verifies content when saving; verification uses that receipt's original content.
- `receipt`: `name` identifies an implemented tool. Optional `arguments` match exact string argument values in a completed receipt.
- `metric`: the same receipt filter plus a dotted result `field`, numeric `target`, and display `unit`. A finite result must meet or exceed the target.

Receipt and string checks establish a concrete observation, not independent factual correctness or customer demand. Owner reviews are explicitly owner assertions. Tool-result metrics do not connect an analytics provider. The dashboard creates owner-review criteria; the API also supports automatic checks. All original criteria must pass. A model's final text and closed child tasks cannot substitute for evidence. Blocked dependencies and unmet criteria stay visible as `needs_evidence`.

Starting without a supplied plan creates a planner task. Only that planner receives `mission_status` and `mission_plan`. The latter accepts a JSON `request` containing `expected_revision`, `reason`, and `steps`. Each step declares `key`, `title`, `prompt`, `agent`, `tools`, `depends_on` and optional `priority`. Missing dependencies, cycles, duplicate keys and excess permissions fail atomically. Independent ready tasks may run concurrently. Dependent tasks wait for prerequisite success. Mission priority combines with each step's priority, clamped to -10 through 10.

## Shared limits

The mission budget covers its planners, plan tasks and delegated descendants together:

- `tasks`: total created tasks, including retired work.
- `steps`: aggregate model steps.
- `tool_calls`: reserved attempts, including failures and uncertain calls.
- `model_tokens`: conservative reservations, including retries and failed calls.
- `model_cost_usd`: optional estimated model cost cap. Configured input and output prices are mandatory when set.

Reservations use the same serialized control transaction as task admission and receipts. Existing per-task, worker-tree, daily, queue, provider and action-policy limits still apply. A mission cannot raise those limits. Model cost estimates exclude adapter fees and are not provider billing. Free-text constraints guide the model; machine-enforced boundaries are the tool/agent lists, write permission, deadline, dependencies and budgets.

Every descendant inherits mission authority. Setting `writes=true` does not approve external changes: existing exact approvals remain mandatory. Setting it false blocks external and shared-memory mutations. The generic task Run endpoint cannot bypass mission dependencies. A client-acquisition mission can plan research and draft an artifact with writes disabled. Sending messages requires both mission permission and the existing exact recipient/message approval.

## Pause, recovery and revisions

`POST /api/missions/{id}/control/{action}` supports start, pause, resume, replan, verify and cancel. Pause stops future work; an active external request can finish. Its receipt remains durable. Cancel and deadline expiry stop the whole mission tree and reject pending approvals. Completed results remain inspectable.

`POST /api/missions/{id}/plan` lets an authenticated owner supply a plan. `replan` asks a new bounded planner to revise it. A revision appends new keys and can retire only unstarted draft steps. It preserves the objective, criteria, existing tasks and external receipts. Concurrent revisions use an expected-version check. An ambiguous write blocks replanning. An identical external action in another mission task is refused and points back to the original receipt. This does not detect differently worded actions with the same semantic effect. Existing within-task stable receipt recovery remains unchanged.

`POST /api/missions/{id}/review` accepts `criterion_id`, `evidence` and `accepted` for owner criteria only. When work stops for missing evidence, **Check completion** evaluates the original criteria. Mission success does not assert independent critic acceptance; that is a separate capability.

## Persistence and evidence

SQLite schema 8 adds missions, membership, plan history and owner reviews. PostgreSQL schema 4 and portable backup format 4 carry the same state. Formats 1–3 remain readable. Existing SQLite owner data migrates in place. Backup restoration keeps the original contract, plan revisions and evidence references.

`tests/test_missions.py` exercises actual task claims, registry calls, artifact writes, dependency order, shared limits, permission refusal, owner review and revision conflicts. The daemon acceptance kills and restarts a real worker process after its source task finishes. It checks that the report completes without repeating the source artifact or plan. A separate test pauses during artifact dispatch and verifies receipt reuse after resume. `tests/test_infrastructure.py` runs a mission against real PostgreSQL and private object storage, then restores and verifies its evidence.

These acceptance tests use explicitly scripted model responses and temporary workspaces. They do not send messages or spend money. Live model mission completion and production operation remain unverified.
