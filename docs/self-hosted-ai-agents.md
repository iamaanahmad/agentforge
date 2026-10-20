# Self-hosted AI agents: what to check before you install

A self-hosted AI agent runs under infrastructure you control. That control also makes you responsible for model access, secrets, updates, and recovery. This guide helps a technical founder decide what to test before trusting an agent with real work.

## What self-hosting gives you

You choose where the application and its data run. You can set network access, storage, backups, and the model accounts the agent uses. Self-hosting does **not** mean every model runs on your machine. A hosted AI provider can still receive prompts and task content when you connect its API.

Ask these questions before installing any agent:

1. **Who can use it?** Check account boundaries and whether one installation serves one owner or a team.
2. **Where does work go?** Find the database, saved files, logs, and backup path. Test restoration, not just backup creation.
3. **What can it change?** List each external tool. Check how approvals bind the exact action and what happens if a request times out.
4. **What does it cost?** Include the host, storage, model calls, and any optional service. A free repository does not make operation free.
5. **Can you inspect the result?** Look for the input, actions, errors, approvals, and saved output. Reopen the work after a restart.

## A first evaluation that reveals the limits

Install the agent on a local machine or disposable host. Use a test workspace and a provider account with spending limits. Keep external writes off. Give it one narrow task with a result you can check, such as a short positioning brief drawn from a product note. Reject unsupported claims. Then restart the application and confirm you can still read the corrected result.

This test checks the path from request to saved result. It does not establish unattended reliability, safe production operation, or customer demand. Run a separate recovery test before you depend on schedules or external writes.

## Where agentforge fits

[Agentforge](../README.md) is an open-source, self-hosted workspace for one owner. You run its web app and worker and supply a supported AI provider account. It keeps tasks, approvals, activity history, and text artifacts in a saved workspace. It supports bounded specialist workers and owner-reviewed missions. See the [feature scope and limits](../README.md#what-you-can-do) and [approval controls](action-policies.md).

The public [fresh-install mission report](fresh-mission-acceptance.md) records one assisted Bedrock mission. The owner rejected an unsupported first draft, directed a correction, and retrieved the accepted file after restart. This is evidence for that one path. It does not prove repeat unattended quality or production use. No agentforge production host has been verified.

For a hands-on test, follow the [self-host guide](self-host-guide.md). It covers install, model setup, a first saved task, and a supervised mission. If you need to coordinate several agents, read [how agent orchestration frameworks differ from an owner workspace](agent-orchestration-frameworks.md).
