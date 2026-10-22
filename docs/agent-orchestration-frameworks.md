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

## Compare the options buyers ask about

LangGraph, n8n, and CrewAI are common options for this job. Use the distinctions below to choose what to test. The linked sources describe each product's own scope.

| Option | When to consider it | What you would set up |
|---|---|---|
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) | You want to build agent coordination in code. Its checkpoints support saved state, and [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) pause work for human input. | Build the owner-facing task view, approval flow, and business integrations your use case needs. |
| [n8n](https://docs.n8n.io/) | You want a visual workflow tool with app connections. It supports self-hosting and [human review of AI tool calls](https://docs.n8n.io/advanced-ai/human-in-the-loop-tools/). | Design the workflows and connect each app. n8n calls its license fair-code, so check its terms if open-source licensing matters to you. |
| [CrewAI](https://docs.crewai.com/core-concepts/Agents) | You want role-based agent teams and structured Flows. [CrewAI AMP](https://docs.crewai.com/enterprise/introduction) adds a separate deployment and monitoring platform. | Define agents and Flows. Decide whether you need the separate AMP platform for the owner view. |
| [agentforge](../README.md#what-you-can-do) | You want one owner's tasks, approvals, activity history, and saved results together. | Run the web app and worker, connect your AI provider, and use the [self-host guide](self-host-guide.md). Only one supervised mission has passed a fresh-install live-model check; production use is unverified. |

These products overlap. Test the exact approval, saved-result, and restart path you need before committing to one.

The [fresh-install mission report](fresh-mission-acceptance.md) shows one assisted Bedrock mission that produced a saved brief after an owner-directed correction. It does not test unattended multi-agent work across repeated runs. Review the [product scope](../README.md#what-you-can-do) before comparing it with a framework you already use.

To try the owner-workspace path, use the [self-host guide](self-host-guide.md). For hosting tradeoffs and a practical first test, read [self-hosted AI agents: what to check before you install](self-hosted-ai-agents.md).
