"""Synchronous v1 owner SDK. Sessions stay in memory; writes never retry automatically."""

from urllib.parse import quote, urlsplit
import httpx


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        hints = {
            401: "Sign in again",
            403: "Session or origin denied",
            409: "Inspect the current state and configuration",
            422: "Check the v1 request schema",
            429: "Wait for the rate limit",
            503: "Check service readiness",
        }
        super().__init__(f"API {status}: {hints.get(status, 'Request failed; inspect the service')}")


def ident(value):
    return quote(str(value), safe="")


class Client:
    def __init__(self, url="http://localhost:8000", *, timeout=30, transport=None):
        parsed = urlsplit(url)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Use a server origin without credentials, paths or queries")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("Use HTTPS outside loopback")
        self.http = httpx.Client(
            base_url=url.rstrip("/"), timeout=timeout, follow_redirects=False, transport=transport
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            if self.http.cookies:
                self.request("POST", "/logout", {})
        except (APIError, httpx.HTTPError):
            pass
        finally:
            self.http.close()

    def request(self, method, path, body=None, **params):
        response = self.http.request(
            method, "/api/v1" + path, json=body, params=params, headers={"Content-Type": "application/json"}
        )
        if not response.is_success:
            raise APIError(response.status_code)
        return response.json()

    def login(self, password):
        data = self.request("POST", "/login", {"password": password})
        self.http.headers["X-CSRF-Token"] = data["csrf_token"]
        return {"authenticated": True}

    def create_mission(self, spec):
        return self.request("POST", "/missions", spec)

    def mission(self, mission_id):
        return self.request("GET", "/missions/" + ident(mission_id))

    def control_mission(self, mission_id, action):
        if action not in ("start", "pause", "resume", "cancel", "replan"):
            raise ValueError("Unknown mission action")
        return self.request("POST", f"/missions/{ident(mission_id)}/control/{action}", {})

    def cancel_mission(self, mission_id):
        return self.control_mission(mission_id, "cancel")

    def plan_mission(self, mission_id, plan):
        return self.request("POST", f"/missions/{ident(mission_id)}/plan", plan)

    def review_mission(self, mission_id, review):
        return self.request("POST", f"/missions/{ident(mission_id)}/review", review)

    def task(self, task_id):
        return self.request("GET", "/tasks/" + ident(task_id))

    def replay(self, task_id, *, offset=0, limit=100):
        return self.request(
            "GET", f"/tasks/{ident(task_id)}/replay", offset=offset, limit=limit, mode="recorded"
        )

    def debug(self, task_id):
        return self.request("GET", f"/tasks/{ident(task_id)}/debug")

    def events(self, *, after=0, limit=100, task_id=None, through=None):
        params = {"after": after, "limit": limit}
        if task_id is not None:
            params["task_id"] = task_id
        if through is not None:
            params["through"] = through
        return self.request("GET", "/timeline", **params)

    def approvals(self):
        return self.request("GET", "/approvals")

    def decide(self, approval_id, decision):
        if decision not in ("approve", "reject"):
            raise ValueError("Use approve or reject")
        return self.request("POST", "/approvals/" + ident(approval_id) + "/decision", {"decision": decision})

    def readiness(self):
        return self.request("GET", "/readiness")
