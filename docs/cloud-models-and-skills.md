# Cloud models and reusable skills

Agent4Good supports AWS Bedrock Converse and Google Vertex AI Gemini, alongside OpenAI and Anthropic.
These adapters reuse saved task state, tool approvals, cancellation checks, model budgets, and nine specialist workers.
They do not provide every model sold by either cloud. Select a text model supporting that API and function tools.
Cloud credits depend on your account, offer, region, and model. Check coverage before starting paid work.

## AWS Bedrock

1. Enable access to your chosen Converse-compatible model in your AWS account and region.
2. Create a dedicated least-privilege IAM identity with `bedrock:InvokeModel` for the selected model resources.
3. Store credentials in a private JSON file outside the repository: `access_key_id`, `secret_access_key`, and optional `session_token`.
4. Import that file through the existing encrypted vault:

```sh
uv run python -m agent4good.vault put bedrock_credentials --agent strategist --agent product_engineer --agent research_analyst --stdin < /private/bedrock.json
```

These examples allow three roles. Add another `--agent ROLE_ID` for each additional specialist needing this credential.
The vault must already have its external key configured. Restrict the private file to its owner and remove it after import.
Temporary AWS credentials need renewal before expiry. This adapter does not assume roles or refresh STS sessions.
It never inherits the host's AWS credential chain. No Tin credentials are transferred.

Set a profile and all six routes in private server configuration:

```dotenv
A4G_MODEL_PROFILES={"cloud":{"provider":"bedrock","model":"YOUR_CONVERSE_MODEL_ID","region":"us-east-1","max_output_tokens":4096}}
A4G_MODEL_ROUTES={"planning":"cloud","coding":"cloud","browsing":"cloud","research":"cloud","summarization":"cloud","verification":"cloud"}
```

Use the foundation model ID or a supported inference profile ID, not an ARN or endpoint URL.
Cross-region inference profiles can process data outside the selected region. Check the model's routing policy.
Restart web and worker after changing profiles. Existing tasks retain their pinned model; create a fresh task.

## Google Cloud Vertex AI

1. Select your billed Google Cloud project and enable the Vertex AI API.
2. Give a dedicated service account the required Vertex prediction permission, using the smallest practical IAM role.
3. Create its service-account JSON credential and import it from a private file:

```sh
uv run python -m agent4good.vault put vertex_credentials --agent strategist --agent product_engineer --agent research_analyst --stdin < /private/vertex.json
```

Restrict and remove the source file after import. Rotate or revoke the service-account key through Google Cloud.
The adapter refreshes short-lived access tokens from this credential. It accepts only Google's fixed OAuth token endpoint.
It does not use ambient application credentials or external-account federation files.
For deployments prohibiting service-account keys, workload identity support remains a separate requirement.

Use this profile with the same six-route object above:

```dotenv
A4G_MODEL_PROFILES={"cloud":{"provider":"vertex","model":"YOUR_GEMINI_MODEL_ID","project":"YOUR_PROJECT_ID","location":"global","max_output_tokens":4096}}
```

Replace every placeholder. Use a location where your chosen model supports function calling.
This uses your Vertex AI project, not a Google AI Studio API key or a transferred chat subscription.
Partner models on Vertex use different APIs and are not implemented by this adapter.

## Spending and first use

Set provider-side billing alerts and quotas. Configure accurate model prices and `A4G_MAX_TASK_MODEL_COST_USD` for estimated task limits.
These estimates are not a guaranteed invoice ceiling. Requests in flight can still incur costs after cancellation.
No model call runs merely because a credential was imported. Start with a small supervised task.
Ask the strategist to read the product engineer skill and save a short implementation plan as an artifact.
Then try two bounded child workers and inspect their results in the timeline before any external write.
Use existing mission and quality contracts for critical work. A passing test suite does not prove a live mission completed.

## Ten packaged skills

`skill_read` returns versioned instructions for all nine specialist roles and the DataForSEO workflow.
The model can load a relevant workflow when needed. Skills grant no tools, credentials, or extra permissions.
The catalog only reads packaged files. Task arguments cannot select arbitrary paths or execute downloaded code.
The strategist coordinates children through existing `worker_spawn`, `worker_wait`, and `worker_results` tools.
This release does not implement arbitrary MCP servers or third-party skill installation.

## DataForSEO

Store a private JSON object with `login` and `password` as `dataforseo_credentials`:

```sh
uv run python -m agent4good.vault put dataforseo_credentials --agent strategist --agent organic_growth_engineer --agent research_analyst --stdin < /private/dataforseo.json
```

Set a trusted upper-bound per-request cost in the owner action policy's `tool_costs_microusd.dataforseo_search_volume`.
Read the current policy and preserve existing rules when updating it through `/api/policy`.
The unit is one millionth of a US dollar. Use your actual account price; no default price is assumed.
Without an estimate, financial requests are denied. Even with an estimate, every exact request requires owner approval.

`dataforseo_search_volume` accepts a JSON string containing 1–20 keywords, a numeric location code, and a two-letter language.
It calls only Google Ads live search volume. Null volume remains unknown. Results are untrusted source data.
Requests use durable write receipts because they cost money. Ambiguous requests are held, not automatically repeated.
SERPs, backlinks, rank tracking, and full-site crawls are not part of this adapter.

## Evidence and limits

Controlled tests cover cloud transcripts, signed-state replay, input validation, cancellation, and paid-query approval and deduplication.
Live cloud access, credit coverage, model quality, and production operation require your credentials and host.
Browser and coding tools still need their private Docker brokers. This release does not install infrastructure automatically.
Cloud adapters currently provide text and local function tools. Vision, streaming, and schema-constrained cloud final output are unavailable.

Sources: [Bedrock Converse](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html),
[Vertex function calling](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/function-calling),
[Vertex thought signatures](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures),
[DataForSEO search volume](https://docs.dataforseo.com/v3/keywords_data-google_ads-search_volume-live/).
