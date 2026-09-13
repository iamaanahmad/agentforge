# Browser control

`browser_run` executes a bounded, exact owner-approved journey in real Chromium.
It uses the normal registry, scoped policies, durable receipts, rate limits, and task cancellation.
Page text and screenshots are untrusted observations, not instructions or independent verification.

## Isolation and setup

Run the trusted broker on a dedicated, patched Linux Docker host without valuable files or company credentials.
The model worker receives only a private Unix socket. It never receives Docker access.
Each journey creates a disposable container with no network attachment, host mounts, or provider secrets.
The image is read-only. Temporary storage is capped at 256 MiB; memory at 1 GiB; CPU at one core.
The container has 256 process slots, dropped capabilities, and no privilege gain.
Chromium uses Playwright's default container launch configuration. This is container isolation, not an additional Chromium sandbox guarantee.
Containers share a kernel. Use a disposable VM for hostile workloads; multi-tenant operation remains unsupported.

Build and review the image, then pin its local digest:

```sh
docker build -f browser/Dockerfile -t agent4good-browser:local .
docker image inspect agent4good-browser:local --format '{{.Id}}'
mkdir -m 700 /srv/a4g-browser-state /run/a4g-browser
# Create the key outside session state. Back it up separately; never pass it to Chromium.
(umask 077; head -c 32 /dev/urandom > /srv/a4g-browser.key)
uv run python -m agent4good.browser \
  --socket /run/a4g-browser/browser.sock \
  --image sha256:REPLACE_WITH_REVIEWED_IMAGE_ID \
  --state /srv/a4g-browser-state --key-file /srv/a4g-browser.key \
  --host your-owned-site.example
```

Set `A4G_BROWSER_SOCKET` on the model worker to the private socket.
Use a supervised service, one broker per dedicated Docker daemon, and an owner-only socket directory.
The broker removes abandoned labelled containers at startup and shutdown.
OS access to the socket is trusted. Never expose it over public HTTP.
No production broker host is included or automatically provisioned.

## Journey contract

The tool takes one string argument, `journey`, containing this JSON:

```json
{
  "steps": [
    {"action": "navigate", "target": "https://your-owned-site.example/", "value": ""},
    {"action": "inspect", "target": "", "value": ""},
    {"action": "screenshot", "target": "", "value": ""}
  ],
  "writes": [],
  "reset_session": false
}
```

Maximum 20 steps, eight write permits, four tabs, and 95 KB total input.
`target` is a CSS locator except navigation URLs and zero-based tab indexes.
Locators resolve again at action time. No arbitrary JavaScript or host file paths are accepted.

| Action | Target and value |
|---|---|
| navigate, new_tab | Target is an HTTPS URL |
| click, scroll, wait | Target is a CSS locator |
| fill, select | Target is a CSS locator; value is the text or option value |
| check | Target is a CSS locator; value is `true` or `false` |
| press | Target is a CSS locator; value is Enter, Tab, Escape, ArrowDown, or ArrowUp |
| upload | Target is a file input; value is JSON with `name`, `mimeType`, and `base64` |
| download | Target is the link/button producing a browser download |
| switch_tab | Target is a zero-based current tab index |
| close_tab | Close current tab; keep at least one |
| inspect | Return bounded text, control selectors, URL, tab URLs, and step status |
| screenshot | Save the current viewport as PNG evidence |

Uploads accept at most 40 KB of inline bytes, never a host path.
Downloads accept at most 80 KB. Screenshots accept at most 500 KB.
Artifacts use private base64 storage with a decoded SHA-256. Tool result links return the decoded file as a download.
The authenticated endpoint uses attachment disposition and non-executable octet-stream content. Never execute downloaded files.
A screenshot captures a viewport, not an unlimited full page. Total artifacts are capped.

## Network and exact writes

The offline renderer cannot connect to any host, including DNS, localhost services, or metadata endpoints.
Every intercepted HTTP request goes over a bounded pipe to the trusted relay.
The relay permits only configured exact public HTTPS hostnames on port 443.
It resolves public addresses and pins the selected address while verifying the original TLS hostname.
It checks popups, subresources, forms, and downloads through the same path.
HTTP redirects are held before Chromium follows them. Inspect the destination and request explicit navigation.
Service workers and WebSockets are blocked. Unhandled protocols cannot escape the offline container.
There is no host proxy inheritance, arbitrary authorization header forwarding, or account-challenge bypass.
A site requiring a blocked service must stop; it does not receive a broader allowlist automatically.

All journeys require exact owner approval, including reads, since some GET URLs can change remote state.
Use only known safe GET destinations. Do not disguise external actions as navigation.
Every POST, PUT, PATCH, or DELETE also needs an unused exact permit:

```json
{"method":"POST","url":"https://your-owned-site.example/submit","body_sha256":"SHA256_OF_EXACT_REQUEST_BODY"}
```

The default digest hashes the request body bytes, including exact form encoding.
For multipart uploads, `body_digest` hashes ordered part names, filenames, content types, and base64 bytes.
This excludes only the browser-generated boundary. The helper in `agent4good.browser` is the canonical calculation.
If the exact body cannot be prepared safely, do not submit. A guessed permit fails closed.
A changed CSRF token, unknown body, or changed destination requires a newly reviewed journey.
Authenticated APIs requiring arbitrary bearer headers are not supported.

The broker consumes a permit before network I/O. It durably records a request fingerprint within the task scope.
A new journey ID cannot repeat an already attempted identical write in that task.
This includes uncertain submissions where the server may have accepted a request before the response failed.
The broker retains that record across restarts. Session reset does not erase it.
There is no automatic receipt lookup or replay override. Inspect the remote outcome before any new task.
Do not create a new task merely to bypass a held submission.

## Sessions, recovery, and limits

Session state contains cookies and local storage, encrypted with AES-GCM using a separate external key.
Authenticated encryption binds the state to tenant, environment, owner, and task.
It also binds saved results to that scope and the exact journey ID.
Separate tasks cannot select or import another task's scope. Current operation remains single-owner.
The broker processes one journey at a time and commits the result and session together.
Save the encrypted SQLite state and its external key separately while the broker is stopped.
Deleting the broker state loses deduplication evidence; never do that as a retry procedure.

A resumed journey starts with restored browser storage and a blank tab.
Tab URLs are recorded, but pages are not automatically revisited because navigation may repeat a GET submission.
Explicitly navigate again inside an approved journey. Unsaved form fields, sessionStorage, IndexedDB, and live DOM state do not resume.
`reset_session: true` discards prior storage for the next journey without clearing receipts.

Playwright waits for fresh actionable locators. Passive inspection and waits may retry once.
Navigation, clicks, key presses, uploads, and other interactions never retry automatically.
Dialogs are dismissed and recorded; accepting dialogs is not supported.
A timeout returns structured failed observations and stops the remaining steps.
Blocked requests stop the journey. A failed write response marks the result uncertain.
`status: ok` means all requested steps completed, not that a business outcome is independently correct.
Check page observations and network receipts before reporting success.
The outer deadline is 180 seconds, with a 200-second container lifetime and bounded request sizes/counts.
Cancellation removes the container. An in-flight remote request may finish; inspect its receipt before resuming.
A broker crash leaves an incomplete receipt held, not retried. Results generated before the crash may be lost.

## Verification

`uv run pytest tests/test_browser.py -v` runs policy and state checks.
The Browser acceptance workflow builds the actual image and runs the same tests with real containers:

```sh
A4G_TEST_BROWSER_IMAGE=sha256:REVIEWED_IMAGE_ID uv run pytest tests/test_browser.py -v
```

The controlled journey uses a real HTTP server and real Chromium through the relay.
Only the fixture transport replaces public DNS/TLS with a local test server; production checks stay strict.
Tests exercise forms, actual multipart uploads, downloads, popups, cookies after restart, task isolation,
redirect/subresource denial, stale locators, dismissed dialogs, timeouts, cancellation, lost submission responses, and replay prevention.
These are scripted runtime tests. They do not establish live model completion or production operation.

References: [Playwright network routing](https://playwright.dev/python/docs/network),
[Playwright authentication state](https://playwright.dev/python/docs/auth),
and [Playwright container constraints](https://playwright.dev/python/docs/docker).
