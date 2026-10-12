"""Run the web and SQLite worker on one machine with one persistent volume."""

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def configure_origin():
    origin = os.environ.get("A4G_PUBLIC_ORIGIN")
    if not origin:
        app = os.environ.get("FLY_APP_NAME")
        if not app:
            raise SystemExit("Set A4G_PUBLIC_ORIGIN to the public HTTPS URL")
        origin = f"https://{app}.fly.dev"
        os.environ["A4G_PUBLIC_ORIGIN"] = origin
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise SystemExit("A4G_PUBLIC_ORIGIN must be a bare HTTPS origin")
    os.environ.setdefault("A4G_ALLOWED_HOSTS", f'["{parsed.hostname}","localhost","127.0.0.1"]')
    os.environ.setdefault("A4G_SECURE_COOKIES", "true")
    os.environ.setdefault("A4G_ENVIRONMENT", "production")


def main():
    configure_origin()
    children = []
    stopping = False
    failed = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    web = subprocess.Popen(
        [
            "uvicorn",
            "agent4good.app:create_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--no-proxy-headers",
        ]
    )
    children.append(web)
    deadline = time.monotonic() + 90
    while not stopping and time.monotonic() < deadline and web.poll() is None:
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=2).close()
            break
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    else:
        stop()
        web.wait(timeout=120)
        raise SystemExit("Web did not become healthy")
    if stopping:
        web.wait(timeout=120)
        return
    worker = subprocess.Popen([sys.executable, "-m", "agent4good.worker"])
    children.append(worker)
    while not stopping:
        if any(child.poll() is not None for child in children):
            failed = True
            stop()
            break
        time.sleep(1)
    for child in children:
        if child.poll() is None:
            child.wait(timeout=120)
    if failed or any(child.returncode not in (0, -signal.SIGTERM) for child in children):
        raise SystemExit("A hosted process stopped unexpectedly")


if __name__ == "__main__":
    main()
