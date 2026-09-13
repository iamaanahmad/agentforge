# Model routing

Agent4Good implements OpenAI Responses, Anthropic Messages, AWS Bedrock Converse, and Google Vertex AI Gemini adapters.
These send real HTTPS requests when their server credential is available.
Tests use scripted HTTP responses and do not establish account or model availability.

## Configure routes

Existing `A4G_MODEL`, `A4G_OPENAI_API_KEY`, and `A4G_MAX_OUTPUT_TOKENS` remain supported.
Unassigned work types use this legacy OpenAI profile.
No credentials, hostnames, or fallback destinations belong in model profiles.

Set JSON objects in the server environment or `.env`:

```dotenv
A4G_MODEL_PROFILES={"default":{"provider":"openai","model":"gpt-5.4-mini"},"researcher":{"provider":"anthropic","model":"YOUR_CLAUDE_MODEL","max_output_tokens":4096,"timeout_seconds":60}}
A4G_MODEL_ROUTES={"planning":"default","coding":"default","browsing":"default","research":"researcher","summarization":"default","verification":"default"}
```

Replace `YOUR_CLAUDE_MODEL` with a Messages-compatible model available to your account.
Each route can name a different profile, model, and provider.
Profiles cannot contain endpoint URLs or undeclared fields.
Unknown providers, work types, and profile references fail startup validation.
Restart both web and worker processes after changing settings.

Set `A4G_ANTHROPIC_API_KEY` for Anthropic or use the encrypted credential vault.
The vault accepts `anthropic_api_key` with the `model` purpose.
Existing tenant, environment, role, revocation, and redaction rules apply.
Never place credentials in task prompts, browser settings, or shared memory.

The task form offers an explicit work type. The API accepts the same `work_type` field.
Omitting it uses the agent default:

| Agent | Default work type |
|---|---|
| Product engineer | coding |
| Research analyst, organic growth, outreach | research |
| Customer support | summarization |
| Product analyst | verification |
| Other roles | planning |

Select `browsing` explicitly when needed. This selects a model, not a browser tool.
Schedules inherit their role's default; this release does not add per-stage mission routing.
One task retains one profile throughout execution.

## Contract and capabilities

The adapters normalize assistant text, local function calls, tool results, usage, and errors.
They support sequential client-side tools. Server-side tools, streaming, and vision are unavailable.
Profiles declare `tools` and `structured_output` support; declarations do not verify model access.
The runtime needs tools, so a profile with `tools=false` cannot start a task.
Provider rejections remain errors, including incorrect owner capability declarations.

OpenAI supports optional schema-constrained final text through `respond(..., schema=...)`.
The adapter validates returned JSON against that schema locally.
Anthropic schema-constrained final output is unavailable in this adapter.
Selecting it fails configuration validation. This is an adapter limit, not a vendor capability claim.
The normal task loop does not require a final-output schema.

Incomplete output, unsupported content, invalid tool arguments, and refusal fail without executing tools.
Anthropic translates tool IDs and results into Messages blocks.
It refuses opaque OpenAI reasoning items instead of silently dropping them.

## Recovery and failure boundaries

The first model call saves the complete profile with the task.
Later configuration changes cannot switch an existing task's provider or model.
This remains true after approval pauses, completed writes, retries, and restarts.
There is no automatic provider or model fallback, before or after external actions.
Migrated transcripts without a saved route cannot switch away from the legacy profile.
Create a new task to use another route; inspect previous receipts before repeating any external action.

Transport failures and selected temporary HTTP failures use the runtime's bounded retry count.
Authentication, capability, validation, and budget failures do not retry.
Each attempt consumes the task step and reservation budgets.
Cancellation is checked before and after requests. A request already underway may finish and incur cost.
No returned tools execute after cancellation. Requests have bounded HTTP timeouts, not forceful remote cancellation.

## Resource and spending limits

Profiles bound output tokens (256–16,000), input bytes (1,024–1,000,000), and timeout (1–120 seconds).
`A4G_MAX_TASK_MODEL_RESERVED_TOKENS` defaults to 2,000,000.
Reservations count input UTF-8 bytes, protocol headroom, and maximum output tokens.
This conservative workload estimate is not provider tokenization.
Reservations persist across restarts and remain charged after failed or ambiguous requests.
Provider-reported token usage is saved separately in the authenticated task detail API.

An optional `A4G_MAX_TASK_MODEL_COST_USD` requires explicit `input_usd_per_million`
and `output_usd_per_million` prices on each selected profile.
An unpriced previous call blocks a newly enabled cost cap for that task.
The cap uses configured estimates, not provider billing or a guaranteed invoice ceiling.
It excludes non-model tools and depends on accurate prices, including cache and reasoning charges.
Use provider-side spending controls as well.

## Verification status and acceptance

The Connections page and `/api/readiness` show all six routes.
Missing credentials mean unavailable. Configured credentials mean unverified until a call succeeds.
A saved successful call marks that provider/model pair as having call evidence in this database.
That evidence is historical; it does not certify current credentials or every capability.
Copied test databases must not be treated as live evidence.

Release acceptance used isolated, scripted HTTP services for both adapters.
Coverage includes six independent routes, tool round trips, safe errors, cancellation,
capabilities, schema validation, timeouts, retry bounds, durable budgets, and route pinning.
Neither an OpenAI nor an Anthropic credential was present in the release workspace or vault.
No live provider completion or production deployment is claimed.

To obtain live acceptance, configure the chosen account's key and model.
Start a short, explicitly tagged task with no requested external writes.
Inspect its final text, selected route, model calls, usage, and task journal.
Retain the task as a labeled test record. A text result alone does not validate every tool capability.

Protocol references: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling),
[OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
and [Anthropic client tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).

Cloud credential setup, model scope, and reusable skills: [cloud models and skills](cloud-models-and-skills.md).
