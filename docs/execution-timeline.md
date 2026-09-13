# Execution timeline

The Timeline page keeps the existing Activity page and adds a linked view of saved work.
Choose a task to see its goal, child agents, current states, saved plans, decisions,
results, verification, and artifact links. Filter events by task tree, role, or event type.
The browser polls every five seconds. It retains visible records during a disconnect,
then resumes from the last saved event ID. Open disclosures survive polling.

## Record contract

Authenticated `GET /api/timeline` accepts `task_id`, `agent`, `kind`, `after`, `limit`
(1–500), and `through`. It returns version 1 events, task snapshots, `next_cursor`,
`has_more`, `through`, and `captured_at`. Event IDs determine order; timestamps are
presentation metadata. SQLite write serialization and the PostgreSQL control lock
ensure committed order. The API uses a consistent database snapshot for each page.

Structured events use an allowlisted versioned JSON envelope in the existing event
message column. New plan steps, observations, model reservations and outcomes,
approval decisions, delegation, tool attempts, and completion carry their relevant
step, tool, model-call, approval, and child identifiers. Task projections add root,
parent, mission, and role identifiers. Existing plain activity remains readable.
There is no invented historical backfill, schema migration, or hidden reasoning feed.

Export record downloads filtered JSON. Its first page fixes a high watermark;
later pages cannot include events created after that watermark. Task metadata is
captured during export and is not a historical replay of mutable task fields.
Polling deduplicates by event ID. Repeated completed observations do not add another
visible action. Separate retry attempts remain distinct records.

## Usage and trust boundaries

Tokens come from saved provider responses. Missing responses produce partial or
unavailable totals. Reserved tokens are limits, never reported consumption. Model
costs use the pinned route's configured price estimates only when all required usage
is known. Actual provider billing and tool charges remain unavailable. Child usage
is shown on each child, not silently added to a parent total. Elapsed duration is
wall time from execution start, including waits; it is not active CPU time.

Every request requires owner authentication. Foreign-owned tasks return 404.
Credential redaction covers event messages and all projected task data. Raw tool
arguments, pending payloads, provider transcripts, and hidden reasoning are excluded.
Owner goals and final outputs remain visible, so exported records are private work
records. This is a single-owner interface, not a multi-tenant audit service.

## Verification

`tests/test_timeline.py` covers authentication, owner filtering, redaction, bounded
pagination, concurrent commits, fixed-watermark export, SQLite backup/reopen,
transaction rollback, observation replay, usage labels, and real parallel Engine
workers. A scripted provider exercises model failure/retry, an authenticated owner
approval, engine recreation, and final evidence. Responses in these tests are
scripted; they do not prove live model quality or production operation.

Live acceptance requires an owner-configured model credential and a bounded task
with delegated work. Compare the exported IDs with the durable event rows, restart
the services, and confirm continuation without duplicate visible actions. No local
provider credential was configured when this feature was built.
