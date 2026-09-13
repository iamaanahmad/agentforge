"""Offline disposable renderer. No credentials or host access; broker handles all HTTPS."""

import base64
import json
import sys

from playwright.sync_api import sync_playwright, Error


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    incoming = json.loads(sys.stdin.readline(1000000))
    journey, saved = incoming["journey"], incoming["state"]
    observations, artifacts, blocked, dialogs = [], {}, [], []
    current_action = ""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context(
            storage_state=saved.get("storage"),
            accept_downloads=True,
            service_workers="block",
            viewport={"width": 1200, "height": 800},
        )
        context.set_default_timeout(3000)
        context.route_web_socket("**/*", lambda route: route.close())

        def route_request(route):
            request = route.request
            emit(
                {
                    "type": "request",
                    "request": {
                        "url": request.url,
                        "method": request.method,
                        "headers": request.all_headers(),
                        "body": base64.b64encode(request.post_data_buffer or b"").decode(),
                    },
                }
            )
            response = json.loads(sys.stdin.readline(2000000))
            if response.get("denied"):
                blocked.append({"url": request.url[:2000], "method": request.method})
                route.abort("blockedbyclient")
            else:
                # CDP supports repeated Set-Cookie values as newline-delimited header strings.
                headers = {}
                for key, value in response["headers"]:
                    headers[key] = headers[key] + "\n" + value if key in headers else value
                route.fulfill(
                    status=response["status"], headers=headers, body=base64.b64decode(response["body"])
                )

        context.route("**/*", route_request)

        def on_page(page):
            page.on(
                "dialog",
                lambda dialog: (
                    dialogs.append({"type": dialog.type, "action": "dismissed"}),
                    dialog.dismiss(),
                ),
            )
            if len(context.pages) > 4:
                page.close()

        context.on("page", on_page)
        page = context.new_page()
        # Restore storage only. Automatic navigation could replay a prior GET form submission.
        status = "ok"
        for index, step in enumerate(journey["steps"]):
            current_action = action = step["action"]
            target, value = step.get("target", ""), step.get("value", "")
            attempts = 0
            try:
                # Only passive DOM inspection/waits may retry. Mutations/navigation never replay.
                limit = 2 if action in {"inspect", "wait"} else 1
                while True:
                    attempts += 1
                    try:
                        if action in {"navigate", "new_tab"}:
                            if action == "new_tab":
                                if len(context.pages) >= 4:
                                    raise ValueError("Tab limit")
                                page = context.new_page()
                            if not target.startswith("https://"):
                                raise ValueError("HTTPS required")
                            page.goto(target, wait_until="domcontentloaded", timeout=10000)
                        elif action == "switch_tab":
                            tab = int(target)
                            if tab < 0:
                                raise ValueError("Invalid tab")
                            if tab >= len(context.pages):
                                context.wait_for_event("page", timeout=3000)
                            page = context.pages[tab]
                        elif action == "close_tab":
                            if len(context.pages) == 1:
                                raise ValueError("Last tab")
                            page.close()
                            page = context.pages[0]
                        elif action == "fill":
                            page.locator(target).fill(value)
                        elif action == "select":
                            page.locator(target).select_option(value)
                        elif action == "check":
                            if value not in {"true", "false"}:
                                raise ValueError("Invalid boolean")
                            page.locator(target).set_checked(value == "true")
                        elif action == "click":
                            page.locator(target).click()
                        elif action == "press":
                            if value not in {"Enter", "Tab", "Escape", "ArrowDown", "ArrowUp"}:
                                raise ValueError("Unsupported key")
                            page.locator(target).press(value)
                        elif action == "scroll":
                            page.locator(target).scroll_into_view_if_needed()
                        elif action == "wait":
                            page.locator(target).wait_for(state="visible")
                        elif action == "upload":
                            file = json.loads(value)
                            if set(file) != {"name", "mimeType", "base64"} or any(
                                c in file["name"] for c in "/\\"
                            ):
                                raise ValueError("Invalid upload")
                            data = base64.b64decode(file["base64"], validate=True)
                            if len(data) > 40000:
                                raise ValueError("Upload limit")
                            page.locator(target).set_input_files(
                                {"name": file["name"], "mimeType": file["mimeType"], "buffer": data}
                            )
                        elif action == "download":
                            with page.expect_download(timeout=10000) as event:
                                page.locator(target).click()
                            download = event.value
                            path = download.path()
                            with open(path, "rb") as stream:
                                data = stream.read(80001)
                            if len(data) > 80000:
                                raise ValueError("Download limit")
                            artifacts[f"download-{index}.bin"] = base64.b64encode(data).decode()
                            download.delete()
                        elif action == "screenshot":
                            data = page.screenshot(type="png", timeout=5000)
                            if len(data) > 500000:
                                raise ValueError("Screenshot limit")
                            artifacts[f"screenshot-{index}.png"] = base64.b64encode(data).decode()
                        elif action != "inspect":
                            raise ValueError("Unknown action")
                        break
                    except Error:
                        if attempts >= limit:
                            raise
                text = page.locator("body").inner_text(timeout=1000)[:3000] if action == "inspect" else ""
                elements = []
                if action == "inspect":
                    elements = page.locator("a,button,input,select,textarea,form").evaluate_all("""els => els.slice(0,40).map(e => {
                        function selector(n) {
                            if(n.id) return '#' + CSS.escape(n.id);
                            if(!n.parentElement) return n.tagName.toLowerCase();
                            let peers = [...n.parentElement.children].filter(x=>x.tagName===n.tagName);
                            return selector(n.parentElement)+' > '+n.tagName.toLowerCase()+':nth-of-type('+(peers.indexOf(n)+1)+')';
                        }
                        return {selector:selector(e),tag:e.tagName.toLowerCase(),type:e.getAttribute('type'),
                            name:e.getAttribute('name'),text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,150),
                            href:e.tagName==='A'?e.href.slice(0,2000):null,
                            form:e.tagName==='FORM'?{action:e.action.slice(0,2000),method:e.method}:null,
                            checked:e.type==='checkbox'?e.checked:null,
                            options:e.tagName==='SELECT'?[...e.options].slice(0,20).map(o=>({value:o.value.slice(0,150),text:o.text.slice(0,150),selected:o.selected})):[]};
                    })""")
                observations.append(
                    {
                        "step": index,
                        "action": action,
                        "status": "ok",
                        "attempts": attempts,
                        "url": page.url[:2000],
                        "text": text,
                        "elements": elements,
                        "tabs": [p.url[:2000] for p in context.pages],
                    }
                )
            except Exception:
                observations.append(
                    {
                        "step": index,
                        "action": current_action,
                        "status": "failed",
                        "attempts": attempts,
                        "error": "Action failed; inspect page and network before a new journey",
                    }
                )
                status = "failed"
                break
            if blocked:
                status = "blocked"
                break
            if len(json.dumps(artifacts)) > 1500000:
                status = "artifact_limit"
                break
        state = {"storage": context.storage_state(), "tabs": [p.url[:2000] for p in context.pages]}
        context.close()
        browser.close()
    emit(
        {
            "type": "result",
            "state": state,
            "result": {
                "status": status,
                "observations": observations,
                "artifacts": artifacts,
                "blocked": blocked,
                "dialogs": dialogs,
                "untrusted_source": True,
            },
        }
    )


if __name__ == "__main__":
    main()
