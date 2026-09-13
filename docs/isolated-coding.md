# Isolated coding workspaces

Agent4Good can run an approved coding script in a fresh, offline container.
The model worker never runs the script or receives a Docker socket.
A separate trusted broker controls Docker through a private Unix socket.

## Supported workflow

1. Configure one repository, a reviewed toolchain image, and the private broker socket.
2. Call `sandbox_run` with an exact Git commit, script, and requested artifact paths.
3. Review the exact script through the existing task approval system.
4. The control process reads the configured repository through GitHub's API.
5. The sandbox initializes a secret-free source snapshot, clones it, and creates `agent4good/work`.
6. The script edits files, installs offline dependencies, runs checks, and builds outputs.
7. The broker exports bounded artifacts and removes the container, processes, and temporary files.
8. The worker stores artifact attachments and the durable execution receipt.

Source snapshots preserve UTF-8 file content, not repository history, executable modes, or remote credentials.
Submodules, symlinks, files above 100 KB, and snapshots above 700 KB are refused.
There can be at most 128 source files. Truncated GitHub trees are refused.
For larger repositories, set `A4G_SANDBOX_SOURCE_PREFIX` to a reviewed subdirectory.
The model cannot choose this prefix or another repository.
Dependencies must exist in the reviewed image or its wheelhouse.
Network installation is deliberately unavailable during generated execution.

Each tool call creates a new workspace. Export files before the call finishes.
Artifacts are base64 text attachments, with their decoded SHA-256 in the tool result.
Decode an attachment as data. Never automatically execute or extract it on your control host.
The combined artifact limit is 80 KB, across eight files.
Logs are truncated to 12,000 characters. Source input and result values remain untrusted evidence.
An exit code or script-written test report is not an independent correctness verdict.

## Start the broker

Use a dedicated, patched Linux sandbox host or disposable VM without company credentials or valuable data.
Run the model worker in a separate container with only the private broker socket directory mounted.
Map its socket user to the broker owner UID. Never mount the Docker socket or broker files into that worker.
The container engine requires working memory, PID, and CPU cgroup enforcement.
Preflight refuses engines that report these controls unavailable.

```sh
# Build this trusted image before accepting generated work.
docker build -f sandbox/Dockerfile -t agent4good-sandbox:local .
docker image inspect agent4good-sandbox:local --format '{{.Id}}'
# Use the full sha256 image ID from the preceding command.
mkdir -m 700 /run/user/1000/agent4good-sandbox
uv run python -m agent4good.sandbox \
  --socket /run/user/1000/agent4good-sandbox/broker.sock \
  --image sha256:REPLACE_WITH_REVIEWED_IMAGE_ID
```

Set `A4G_SANDBOX_SOCKET` in the model worker to that private socket path.
Keep its parent directory owner-only. The socket is created with private permissions.
Run the broker under a supervised service. Do not expose its API over public HTTP.
The API trusts OS socket access. Anyone with that access can submit bounded jobs.
Task policy and credential scopes are enforced by the model worker before submission.
Use one broker per dedicated Docker daemon. Its startup recovery removes all labelled abandoned sandboxes.
A process lock prevents two brokers using the same socket directory.
Different socket directories sharing a Docker daemon are unsupported.

Optional `--runtime runsc` selects an installed gVisor runtime.
The default `runc` path has real CI evidence. gVisor has not been tested here.
A reviewed image must contain no secrets and declare no Docker volumes.
The broker refuses tag-based image selection, image volumes, and unsupported resource controls.
Do not mount Docker's socket into the web or model-worker container.

## Enforced limits

| Boundary | Fixed behavior |
|---|---|
| Identity and privileges | UID/GID 10001, all capabilities dropped, no privilege gain, default seccomp |
| Filesystem | Read-only image; no host mounts; 128 MiB workspace and 16 MiB temporary directory |
| Network | No network attachment, host network, DNS access, or package downloads |
| Resources | One CPU, 256 MiB memory including swap allowance, 64 processes, 128 file descriptors |
| Files | 16 MiB per-file limit; bounded source, logs, and exported artifacts |
| Time | 120-second broker job default, capped at 300 seconds in backend configuration |
| Capacity | One active job per broker, bounded retained job entries |
| Cancellation | Worker polls task state; broker removes the entire process namespace and workspace |
| Recovery | Startup removes labelled containers; container lifetime also expires if the broker dies |

The broker keeps credentials out of subprocess environments.
It passes no host environment to generated scripts.
The script cannot choose Docker flags, mounts, networks, image, runtime, resource caps, or control commands.

A kernel or container-runtime vulnerability can still cross a container boundary.
These checks do not prove immunity to unknown escapes.
Use a disposable VM or stronger isolation for adversarial multi-tenant workloads.
This release remains single-owner. The local workspace used for development denies user namespaces and has no Docker engine.
Real container acceptance therefore runs on an ephemeral GitHub-hosted Linux runner.
No production sandbox host has been configured or verified.

## Restricted container builds

The image includes `/opt/a4g/image.py`, a small Dockerfile packager.
Run it after compiling your files inside the sandbox:

```sh
python3 -I /opt/a4g/image.py Dockerfile image.tar
```

It supports `FROM scratch`, individual local `COPY` files, and JSON `CMD` or `ENTRYPOINT`.
It creates a Docker-loadable image archive without a Docker socket, daemon, or network connection.
`RUN`, remote `ADD`, external base images, stages, build secrets, mounts, and arbitrary Dockerfile frontends are refused.
Compile or generate files with the sandbox script before packaging them.
The archive targets Linux amd64 and uses UID 10001.
This is a restricted container-build service, not full Docker BuildKit compatibility.
Large images exceed the current artifact limit and are refused.
CI loads a controlled archive into Docker and checks its metadata.
Production never loads generated images into the control host's engine.

## GitHub delivery

Existing branch writes and draft PR tools retain exact approval requirements.
Exported files do not automatically publish themselves.
Use approved `github_write_file` calls for chosen file content, then `github_open_pr`.

`github_pr_status` records the current PR SHA and actual check results.
`github_merge_pr` requires an exact approved SHA, a same-repository `agent4good/` branch,
and an open, ready PR targeting the default branch.
Set `A4G_GITHUB_MERGE_CHECKS` to a nonempty JSON array of required check names.
Every returned run with a required name must pass. Missing, failed, or excessive check results fail closed.
GitHub branch protection remains authoritative. Configure it to require your trusted checks.
The adapter also rejects PRs that change workflow or credential paths, including renamed paths.
It does not make draft PRs ready automatically.

Set `A4G_GITHUB_DEPLOY_WORKFLOWS` to an explicit JSON list of trusted workflow filenames.
`github_dispatch_workflow` requires an approved SHA that still matches the default branch.
It creates `agent4good-deploy/<sha>` and dispatches that tag, so execution does not follow a moving branch.
Existing tags cause a refusal rather than a silent repeat. Inspect them before retrying.
Repository administrators must prevent other actors moving these deployment tags.
Deployment secrets belong to protected GitHub environments, never the coding sandbox.
Workflow acceptance is not deployment success.
Use `github_workflow_status` to record actual run IDs, states, conclusions, and URLs for the approved SHA.
Correlate dispatch event and tag in GitHub before asserting a specific deployment completed.

For short-lived access, configure `A4G_GITHUB_APP_ID`, `A4G_GITHUB_INSTALLATION_ID`,
and `github_app_private_key` in the encrypted vault (or legacy environment configuration).
The App must be installed on the configured repository with the required permissions.
The broker creates one repository-scoped installation token per tool invocation.
Requested permissions depend on the tool. It attempts revocation in `finally`.
If revocation fails, GitHub's installation-token expiry applies and an event records that limitation.
Tokens and App JWTs are redacted and never enter generated-code environments.
Existing `A4G_GITHUB_TOKEN` remains compatible. Its issuer controls its expiry and scope.
App issuance has simulated API contract coverage; live acceptance uses the platform's leased repository token.

## Evidence and recovery

Run the local suite with `uv run pytest -q`.
Run real sandbox checks after building the reviewed image:

```sh
A4G_TEST_SANDBOX_IMAGE=sha256:REPLACE_WITH_IMAGE_ID uv run pytest tests/test_sandbox.py tests/test_delivery.py -v
```

The `Isolated coding acceptance` workflow tests actual containers, source cloning, offline installation,
file edits, Python checks, compilation, container packaging, artifact loading, and cleanup.
It also tests denied host access, denied egress, privileges, PID exhaustion, memory abuse, disk abuse,
symlink artifacts, deadlines, cancellation, broker restart recovery, and the private-socket registry path.
Integration tests use scripted model-free calls. No live model completion is claimed.

A crashed mutating tool receipt remains ambiguous under the existing durable runtime.
Inspect the saved receipt and artifacts before creating a fresh task. It never automatically repeats a write.
If the broker dies after an output is generated but before export, that output may be lost.
Restarts guarantee cleanup, not workspace continuation. Source inputs and approved scripts remain in the task record.

References: [Docker resource controls](https://docs.docker.com/engine/containers/resource_constraints/),
[Docker security](https://docs.docker.com/engine/security/), and
[GitHub installation tokens](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app).
