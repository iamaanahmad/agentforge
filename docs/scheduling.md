# Dependency-aware scheduling

Schedules persist trigger definitions, events, occurrence receipts, and their tasks.
The dashboard supports each mode. The owner API and two registry tools use the same validation.

## Trigger contract

| Mode | Required fields | Behavior |
|---|---|---|
| `once` | `at` with an explicit UTC offset | Dispatch once at or after this instant. |
| `recurring` | `timezone`, `local_time` | Daily wall-clock recurrence in an IANA time zone. |
| `deadline` | `at` with an explicit UTC offset | Dispatch a deadline action at this instant, after dependencies finish. |
| `event` | Authenticated events with unique IDs | Dispatch once per event ID within this schedule. |
| `condition` | `condition.task_id`, `condition.status` | Check a local task status on a bounded interval. Dispatch on a false-to-true transition. |
| `interval` | `interval_minutes` | Preserve the original elapsed-time recurrence. |

A deadline trigger schedules an action when a deadline arrives. It does not guarantee completion by that time.
The separate optional `deadline` field is the last permitted dispatch instant for any mode.
Checks use the host's UTC clock. Operators must synchronize host clocks.

Recurring schedules use daily wall time, not cron expressions. The spring-forward missing time is skipped.
The fall-back repeated time runs only on its first occurrence. Stored instants use UTC.
`once` and `deadline` require explicit offsets, avoiding ambiguous local input.
The dashboard converts its date inputs from the browser's time zone to UTC.

`missed=coalesce` creates at most one overdue time occurrence, then advances from now.
`missed=skip` records an overdue occurrence without creating a task after `grace_seconds` (default 60).
Calendar recurrence advances to the next valid daily time. It never replays each missed day.
Event IDs retain separate meaning and are processed one per schedule per tick.
Paused event schedules reject new events. Accepted events remain saved during pauses.

## Dependencies, limits, and cancellation

`depends_on` contains at most 20 existing owner task IDs. Every prerequisite must finish as `done`.
A failed or cancelled prerequisite records `dependency_failed` and disables the schedule.
No new task exists while dependencies remain unfinished. `priority` ranges from -10 to 10.
The real worker queue claims higher-priority tasks first; active tasks are not preempted.

`overlap=forbid` waits while an earlier task remains unfinished, including drafts or approval waits.
`overlap=allow` allows separate occurrences to coexist. Defaults forbid overlap for new schedules.
`max_runs` bounds new schedules to 1–1,000 occurrences, with 100 by default.
Recorded skips and failures also count toward this limit. Agent-created schedules allow at most 100.
The workspace accepts at most 100 active schedules. Each agent root can create at most ten schedules in its lifetime.
The existing queue, daily-run, model, tool, and execution limits still apply to dispatched tasks.
Queue saturation leaves an occurrence pending rather than consuming it.

Conditions only inspect a named local task's terminal status. They do not execute code, SQL, models, or network requests.
Their check interval is at least 15 minutes. An already-true condition fires at its first check.
A later firing requires an observed false state between checks. Intermediate changes may be missed.
Condition checks wait for dependencies and overlap availability before consuming a true transition.

Pausing stops dispatch and preserves tasks. Cancellation disables the schedule permanently and cancels unfinished outputs and descendants.
An external request already in flight may finish. Completed tasks and receipts remain inspectable.
A cancelled schedule cannot resume; create a new schedule instead.

## Owner API

All routes require the existing owner session. Mutations also require the session CSRF token, allowed origin, and JSON content type.
They share existing request-size and rate protections. This is not an unauthenticated webhook endpoint.

- `POST /api/schedules` creates a validated schedule.
- `GET /api/schedules` lists schedules with trigger configuration.
- `GET /api/schedules/{id}` returns configuration and the latest 100 occurrence receipts.
- `PATCH /api/schedules/{id}` with `{"enabled":false}` pauses it; `true` resumes it.
- `POST /api/schedules/{id}/cancel` with `{}` cancels it and unfinished work.
- `POST /api/schedules/{id}/events` with `{"event_id":"delivery-123"}` ingests an event.

A repeated event returns `{"accepted":false}`. IDs are scoped to a schedule, limited to 160 characters.
Each schedule accepts at most 1,000 distinct event IDs. Receipts are retained to prevent replay after restart.
Events carry identity only. They cannot override the schedule's prompt, role, permissions, or priority.

Example owner payload:

```json
{
  "name": "Daily product review",
  "prompt": "Review the supplied product evidence and save a brief.",
  "agent": "strategist",
  "mode": "recurring",
  "timezone": "America/New_York",
  "local_time": "09:00",
  "depends_on": [],
  "priority": 3,
  "max_runs": 30
}
```

## Agent authority

`schedule_create` accepts a `request` string containing the owner payload schema.
It requires an explicit matching `allow` rule scoped to the `schedule_create` tool.
A general WRITE allowance does not authorize future work. Deny rules and budget reservations still win.
Manual mode's approval requirement does not become a grant to create unattended work.
`schedule_cancel` can cancel only schedules created by the calling task.
Both tools commit their result and receipt within the same transaction.

Only independent root tasks can create schedules. Mission members, child workers, and scheduled outputs cannot create more schedules.
This prevents recursive schedule growth and escaping mission budgets.
Each occurrence rechecks the creator's current scheduling policy and reserves its configured policy budget.
A denied grant disables the schedule and records `policy_denied`.
The creator may finish normally before its future work runs. Failure or cancellation stops its future and unfinished scheduled work.
Outputs inherit the creator's tool allowlist. Policy checks also apply creator task and role scopes to outputs and descendants.
Vault credential access checks both the output role and the creator role.
Existing exact approvals for external writes remain required. No model-provider key is created by scheduling.

## Persistence and guarantees

SQLite uses `BEGIN IMMEDIATE`; PostgreSQL uses the existing transaction advisory lock.
Task creation, occurrence receipt, and trigger advancement commit together.
A unique `(schedule_id, occurrence)` constraint is the last defense against competing dispatchers.
A crash before commit creates neither a task nor a receipt. A committed receipt survives restart and restoration.
These guarantees concern dispatch into the database. They do not make external effects exactly-once.

SQLite schema 9 and PostgreSQL schema 5 add side tables. The original schedule columns stay intact.
Existing interval schedules migrate without changing IDs, enabled state, interval, or next run.
Legacy intervals retain overlapping and continuing recurrence. New schedules have finite occurrence limits and forbid overlap by default.
Legacy missed intervals still coalesce into one task. Restored older backups migrate their legacy schedules on dispatch.
Portable backup format 5 includes definitions, event IDs, and receipts; restoration accepts older supported versions.

## Verification and limits

`tests/test_scheduling.py` runs all modes through real task storage and workers with explicit scripted model responses.
It covers dependencies, priority, process races, abrupt process exit, replay, cancellation, migration, policy, CSRF, and time changes.
`tests/test_infrastructure.py::test_scheduler_modes_concurrency_and_restore` repeats dispatch and restore checks on real PostgreSQL.
CI supplies PostgreSQL and private object storage. Local tests skip these checks when services are absent.
Live provider completion and production operation require separate acceptance; scripted answers do not establish either.

For rollback, pause schedules and ship a counter-change that restores the prior scheduler UI and dispatch path.
Do not run older code against active new-mode rows: it would treat them as interval schedules.
Keep the new tables and receipts intact. Export or inspect them before any later removal.
