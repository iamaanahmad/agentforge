# Agent orchestration frameworks: choosing the right level of control

An agent orchestration framework coordinates work among AI agents or tools. It may split a request into steps, pass results between workers, and decide when work is complete. The useful question is which parts you want to build yourself and which parts must already be visible to an owner.

## Start with the work, not the worker count

For a single bounded task, one worker with a clear output and a human review can be enough. More workers help only when their jobs have separate inputs and their results must be combined. They also create new failure points: stale shared context, missing child results, duplicate writes, and unclear ownership of the final answer.

Sketch one real workflow before choosing a system:

| Decision | What to verify |
|---|---|
| Work split | Can each child receive a narrow task and return a complete result? |
| Shared context | Can workers read the right facts without overwriting each other? |
| External actions | Does a write need approval for its exact target and content? |
| Failure and restart | Can work resume without repeating an uncertain external write? |
| Final review | Can an owner see the evidence behind the answer and reject a weak result? |

These checks matter more than the number of named roles. A role label does not provide a tool, model credential, or safe permission by itself.

## Framework or owner workspace?

Choose a framework when you want to build and host your own coordination logic inside an application. Check its state, tool, and deployment contracts against your intended workflow. Choose an owner workspace when the main job is to request work, supervise actions, inspect outcomes, and return to saved results. Some teams need both, but adding both increases the parts they must operate.

Agentforge sits on the owner-workspace side. Its [specialist workers](specialist-workers.md) support nine bounded roles, child tasks, messages, and result collection. Its [mission controls](missions.md) let one owner set criteria, review evidence, and direct a correction. The [durable execution guide](durable-execution.md) describes checkpoints and how uncertain writes remain held for inspection. These are documented and locally tested capabilities, not a claim of production reliability or parity with every framework.

The [fresh-install mission report](fresh-mission-acceptance.md) shows one assisted Bedrock mission that produced a saved brief after an owner-directed correction. It does not test unattended multi-agent work across repeated runs. Review the [product scope](../README.md#what-you-can-do) before comparing it with a framework you already use.

To try the owner-workspace path, use the [self-host guide](self-host-guide.md). For hosting tradeoffs and a practical first test, read [self-hosted AI agents: what to check before you install](self-hosted-ai-agents.md).
