# Scoped action policies

Agent4Good checks every supported tool invocation against owner-controlled policy.
The engine requests approval. The registry independently checks authorization before reserving work and again immediately before dispatch.
Direct registry callers cannot skip these checks. Model arguments cannot supply identity, policy, costs, environment, or approval tokens.

## Configure policy

Sign in as the owner. Read `GET /api/policy`, then send `PUT /api/policy` with the returned revision.
The write requires the session cookie and `X-CSRF-Token`, like other owner writes.
There is no policy-editing model tool. Unknown fields, effects, classes, negative limits, and duplicate rule IDs are rejected.
A stale revision returns HTTP 409. Read the latest policy before saving again.

Example request body:

```json
{
  "expected_revision": 1,
  "document": {
    "approval_ttl_seconds": 86400,
    "tool_costs_microusd": {"web_search": 5000},
    "limits": {"calls": 200, "spend_microusd": 1000000, "recipients": 5, "window_seconds": 86400},
    "rules": [
      {"id": "no-production-writes", "scope": {"environment": "production", "action": "WRITE"}, "effect": "deny"},
      {"id": "review-research", "scope": {"user": "owner", "agent": "research_analyst", "tool": "web_search"}, "effect": "approval", "limits": {"calls": 10, "window_seconds": 3600}},
      {"id": "bounded-mail", "scope": {"tool": "send_email"}, "effect": "conditional_approval", "conditions": {"recipients": ["approved@example.org"]}},
      {"id": "escalate-admin", "scope": {"action": "ADMIN"}, "effect": "escalation"}
    ]
  }
}
```

This example does not send anything. Replace addresses with your intended scope before installing it.
`A4G_ENVIRONMENT` sets the trusted server environment. It defaults to `local` for the existing single-host installation.
Set it consistently on web and worker processes. Production hosts should explicitly set `production`.

## Scope and precedence

Each rule may match user, agent, task, tool, environment, and action.
Omitted scopes match all values. Supplied scopes use exact equality and must all match.
Task IDs select individual tasks. Agent values select role IDs, such as `product_engineer`.
The current owner ID is `owner`. Other task owners are denied, because multiple users remain unsupported.

The strictest matching decision wins: deny, escalation, approval, then allow.
Rule order and specificity never override a denial. All matching limits apply together.
A conditional approval needs an exact owner decision and satisfied conditions.
A failed condition denies the action; it does not fall through to another allow rule.
Conditions support recipient allowlists, maximum reserved cost, and exact argument values.

Existing defaults remain a floor:

- Reads and saved artifacts run without approval in supervised or autonomous mode.
- Manual mode requires approval for every tool.
- Workspace memory changes and existing external writes always require exact owner approval.
- An explicit allow cannot remove these existing approval requirements.
- Unknown action classes and invalid owner identities fail closed.

Escalation pauses execution for the sole owner. Approval responses expose `policy.effect` and expiry.
It does not contact anyone or imply a second approver. Multiple approvers require a future identity design.

## Action classes

| Class | Current invocation mapping |
|---|---|
| READ | Workspace reads, HTTPS reads, search, GitHub file and issue reads |
| WRITE | Workspace memory, saved artifacts, GitHub branches and files |
| EXTERNAL_COMMUNICATION | Email, GitHub issue creation, GitHub draft PR creation |
| FINANCIAL | Enforced by the boundary; no financial adapter is implemented |
| DEPLOYMENT | Enforced by the boundary; no deployment adapter is implemented |
| DESTRUCTIVE | Enforced by the boundary; no destructive adapter is implemented |
| ADMIN | Enforced by the boundary; no agent administration adapter is implemented |

Trusted tool specifications declare each class. Catalog discovery exposes that declaration.
Tests exercise all seven declarations through actual registry invocations with isolated test adapters.
This does not establish real financial, deployment, destructive, or admin integrations.
New adapters must declare their class and trusted cost ceiling before becoming executable.
Financial actions without a configured cost ceiling fail closed.

## Shared limits

Global and matched rule limits reserve calls, cost, and recipient deliveries in one SQLite transaction.
The same transaction records execution intent. Separate registry instances share the counters.
A rolling window includes reservations newer than `now - window_seconds`.
Limits use integer micro-US dollars: 1,000,000 means one dollar.

Costs are owner-configured upper bounds per tool invocation, not model estimates or a provider billing ledger.
Unset non-financial tool costs are zero. Set a conservative bound for every paid adapter before relying on spending limits.
Provider model calls occur outside this tool budget. Daily run and model-step limits still apply separately.
Variable-cost integrations need a trusted quotation or maximum-charge contract before this can bound their actual charges.

Recipient limits count delivery attempts, not unique people.
An email consumes one recipient delivery. An issue or PR consumes one repository delivery, identified as `github:owner/repository`.
Repeated messages to the same destination consume additional allowance.

Completed receipt reuse consumes no further allowance. Failed or ambiguous attempts retain their reservations.
Changing the policy revision does not reset global usage or usage under unchanged rule IDs.
Changing a rule ID creates a new rule bucket. Global usage still constrains it.
Owners can intentionally change limits through authenticated policy configuration.
No worker or model can edit the policy through tool execution.

Reservations remain stored for audit. Monitor database growth with backups; this release does not prune policy usage records.
The supported deployment remains one worker on one SQLite host. Concurrency tests establish atomic reservations, not distributed-host support.

## Exact approval and recovery

New approvals bind exact arguments, task, call ID, tool, owner, role, environment, policy version, and expiry.
The version also covers autonomy, tool permissions and class, configured repository, sender, and public origin.
A server signature prevents an edited argument record or expiry from becoming a valid approval.
Changing the session signing secret invalidates existing approval bindings as well as owner sessions.

Both the engine and registry reject stale, altered, unbound, or expired approvals.
Policy changes between reservation and the final check prevent dispatch and leave a held receipt.
Authorization takes effect at the final dispatch check. Cancellation or policy changes cannot retract an external request already dispatched.
Inspect held receipts before starting replacement work. Do not replay uncertain external effects.
Expired or stale approvals fail the run; create a fresh task after checking receipts and approve its exact action.
A policy update never silently reapproves an old action.

Schema migration preserves task IDs, pending decisions, approved decisions, artifacts, and receipts.
Only approval IDs present during the migration receive legacy bindings under the initial default policy.
Their first binding gets the configured expiry. Later startups do not refresh it.
New unbound approvals never receive this migration exception.
Stop old workers before migration; old code does not implement the new checks.
Take an online SQLite backup before restarting web and worker processes together.

## Audit and verification

Decision events record effect and policy version. They exclude rule conditions, messages, recipients, and credentials.
Exact arguments remain in the existing private approval and receipt tables for owner inspection.
Keep backups and database access private. This policy module does not sandbox trusted Python code or protect a compromised host.

Run `uv run pytest -q`, `uv run ruff check .`, and `uv run ruff format --check .`.
Tests cover six scopes, seven classes, real engine approval resumes, owner API authentication, tampering, migration, and concurrent limits.
The integration suite uses simulated external transports. It sends no email and moves no real money.
Run `uv run python scripts/check_registry.py` for a real bounded HTTPS read without model credentials or external writes.
