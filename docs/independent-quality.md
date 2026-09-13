# Independent outcome checks

Critical tasks can require a builder, critic, revision, and separate verifier before completion.
The runtime freezes each candidate and records the evidence each reviewer inspected.
The builder cannot set verdicts or edit reviewer evidence through any tool or owner task API.

This is configured acceptance, not proof of universal correctness. Model agreement never overrides a failed executable check.
Checks only establish the assertions you define. They do not establish untested claims elsewhere in a result.

## Configure a critical task

The authenticated `POST /api/tasks` accepts a `quality` contract, including drafts:

```json
{
  "title": "Write an accurate positioning draft",
  "prompt": "Describe the product and its current evidence limits. Save the final copy in your result.",
  "agent": "strategist",
  "quality": {
    "output_type": "marketing",
    "max_revisions": 2,
    "checks": [
      {
        "id": "availability",
        "description": "The copy must disclose the current live-evidence limit",
        "kind": "text",
        "subject": "result",
        "contains": {"text": "live completion remains unverified"}
      }
    ]
  }
}
```

Choose `software`, `research`, `marketing`, `outreach`, `hackathon`, `deployment`, or `asset` as the output type.
Every contract requires 1–12 checks. Checks have unique IDs and executable assertions.
The contract cannot change after creation. Create a new task if your acceptance requirements change.
A task without a contract keeps legacy execution checks and never claims independent outcome verification.
The dashboard creation form does not yet expose this advanced API field.

`A4G_QUALITY_DEFAULTS` sets mandatory contracts by model work type: `planning`, `coding`, `browsing`, `research`, `summarization`, or `verification`.
Its JSON values use the same contract shape. For example, map `coding` to a software contract.
Defaults are empty for backward compatibility. The server pins defaults before the first model step.
They cover newly executed owner, scheduled, mission, and delegated tasks using that work type.
Existing started tasks and historical results are not retrofitted. Assigned quality reviewers do not recursively review themselves.
Changing configuration cannot remove an already pinned contract.

## Executable checks

| Kind | Subject | Assertions |
|---|---|---|
| `text` | `result`, or an exact saved text artifact name | `equals.text` compares the full text. `contains.text` requires a nonempty literal substring. |
| `receipt` | An actual registry tool name | `equals` compares typed values using dotted paths. `contains` requires literal text at a dotted path. Paths start with `arguments.` or `result.`. |

Receipt checks inspect successful durable tool records, never a builder's claim that a tool ran.
`result.runs.0.conclusion` addresses a field inside an array.
At least one asserted path must reference the observed result.
Bind receipt assertions to the target, revision, URL, recipient, or artifact hash when relevant.
A successful old receipt can satisfy an unbound assertion. The runtime does not infer which revision you intended.

For software, a receipt check can require `sandbox_run`, `result.exit_code: 0`, and `arguments.ref: <exact commit>`.
A zero exit alone is insufficient: also bind `arguments.script` to the required tests or inspect the expected test output.
Scripts still require the existing exact action approval. Quality approval does not authorize execution.

| Output | Concrete verification strategy | Limits to disclose |
|---|---|---|
| Software | Check the sandbox receipt, exact source commit, actual test script, exit code, and exported artifact hashes. | Passing selected tests cannot prove all behavior. Unbound scripts could run no meaningful tests. |
| Research | Check `web_fetch` receipts against exact source URLs and quoted source text. Inspect the saved claims against those sources. | Source contents are untrusted. Presence of a quotation does not prove an inference. |
| Marketing | Compare saved copy with an owner-approved exact statement or required product facts. Review unsupported promises. | Required phrases alone do not prove all copy is correct or on brand. |
| Outreach | Inspect the saved draft against the intended recipient, offer, and required disclosure. If delivery is required, inspect `send_email` and its exact arguments and message ID. | Draft approval proves no delivery. Sending retains exact owner approval. |
| Hackathon | Check published rules through source receipts and required submission fields in saved artifacts. Require the actual browser submission receipt when claiming submission. | Eligibility and acceptance remain unknown without venue evidence. A draft is not a submission. |
| Deployment | Check `github_workflow_status` against the exact SHA, workflow, and successful conclusion. Require actual browser target observations for the deployed behavior. | A workflow dispatch acknowledgement does not prove successful deployment or healthy behavior. |
| Generated asset | Check exported browser/sandbox artifact hashes and expected metadata in actual receipts. Inspect saved text assets against exact content requirements. | There is no visual critic or media generator in this release. Require human visual inspection before claiming visual acceptance. |

These strategies are not fabricated integrations. Missing required tools or evidence cause rejection or a failed review.
For binary aesthetics or judgment outside these checks, keep the acceptance claim narrow and require human review separately.

## Stage and identity boundaries

The builder gets the original task and owner-defined checks.
At finalization, execution checks run first. Active children, incomplete plans, and ambiguous writes still block completion.
The runtime freezes final text, saved artifact versions, and tool receipts into a size-bounded snapshot with a SHA-256 digest.
Object-store reads verify artifact hashes outside the short control transaction.

The critic starts as a new durable worker identity with only `quality_inspect`.
It receives the objective and checks, not the builder transcript, memory, shared context, or messages.
The role stays inside the parent's permissions. The model routes through the owner's `verification` profile.
A distinct task identity enforces independence; a different model is optional and does not prove correctness.

`quality_inspect` evaluates an assertion against the frozen snapshot and writes an ordinary tool receipt.
It returns original evidence, source IDs, the check result, and candidate digest.
A reviewer must inspect every check and return structured findings with reasons.
The runtime recomputes each assertion and requires matching reviewer receipts.
Missing, malformed, or truncated evidence cannot pass. The builder cannot call this tool successfully.

A rejected critic result returns findings to the builder as untrusted data within the original permissions.
The builder revises within the same step, token, cost, deadline, and tree budgets.
Old snapshots, receipts, and findings remain intact. Each revision receives a new critic identity.
After the critic passes, a different verifier independently inspects the same frozen candidate.
The task reaches `done` only after both stages pass against unchanged evidence.

## Failure, recovery, and policy

`max_revisions` accepts 0–3 and defaults to 2. Exhaustion produces `failed`, never a success claim.
A failed critic, invalid report, cancelled review, or verifier disagreement produces an honest failed root task.
Parent cancellation stops unfinished review children. Active provider requests may finish after cancellation.
Reviewers inherit action policy, mission restrictions, worker admission, and shared budgets.
Manual mode may still require approval for inspection reads. Quality never substitutes for policy approval.

Waiting parents use `waiting_children` and release their execution slot.
Restart resumes from the durable snapshot or reviewer checkpoint without creating duplicate stages.
The original elapsed-time deadline includes review waits and revisions.
There is no automatic retry of uncertain external writes and no provider receipt lookup added here.

The authenticated task-detail API exposes the pinned contract, round digests, stages, findings, and reviewer task IDs.
The owner's existing events and task pages show review workers and stage events.
Complete snapshot contents remain private database data and are included in backups.
Snapshots are bounded to 1 MB. A check's returned source evidence is bounded to 20,000 characters.
This release trusts the owner and runtime process. It does not resist direct database edits or a compromised host.

## Migration and evidence

SQLite schema 10 and PostgreSQL schema 6 add three quality tables without changing existing owner results.
Portable backup format 6 includes contracts, immutable snapshots, reviewer identities, and findings.
Formats 1–5 remain readable with empty quality tables.
Stop both services and back up before upgrade. Rollback requires a counter-change retaining quality gates and evidence.
Running older code against critical unfinished tasks would omit the new completion gate.

`tests/test_quality.py` exercises actual engine segments, registry evidence, identity isolation, revisions, denial, cancellation, and recovery.
`tests/test_infrastructure.py` checks PostgreSQL stage execution and restoration.
The CI quality acceptance artifact carries a complete builder-to-critic-to-verifier trace.
Model responses are scripted. These tests do not establish live model quality or production operation.
