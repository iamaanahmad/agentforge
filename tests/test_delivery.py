import pytest

from agent4good.tools import ToolError
from test_tools import permit, runnable


def setup(settings, monkeypatch, changes=None, check="success"):
    settings.github_token = "test-token"
    settings.github_repo = "owner/repo"
    settings.github_merge_checks = ["tests"]
    settings.github_deploy_workflows = ["deploy.yml"]
    r, task = runnable(settings)
    calls = []

    def github(method, endpoint, payload=None):
        calls.append((method, endpoint, payload))
        if method in {"PUT", "POST"}:
            return {"merged": True, "sha": "d" * 40}
        if endpoint == "":
            return {"default_branch": "main"}
        if endpoint.startswith("/git/ref/"):
            return {"object": {"sha": "a" * 40}}
        if "/check-runs" in endpoint:
            return {
                "check_runs": [
                    {"name": "tests", "head_sha": "a" * 40, "status": "completed", "conclusion": check}
                ]
            }
        if "/files" in endpoint:
            return changes or [{"filename": "app.py"}]
        if "/runs?" in endpoint:
            return {
                "workflow_runs": [
                    {
                        "id": 42,
                        "head_sha": "a" * 40,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://github.com/run/42",
                    }
                ]
            }
        return {
            "head": {"sha": "a" * 40, "ref": "agent4good/test", "repo": {"full_name": "owner/repo"}},
            "base": {"ref": "main"},
            "state": "open",
            "draft": False,
            "changed_files": 1,
        }

    monkeypatch.setattr(r, "_github", github)
    return r, task, calls


@pytest.mark.parametrize("check", ["failure", "pending", "cancelled", "skipped"])
def test_merge_requires_passing_checks(settings, monkeypatch, check):
    r, task, calls = setup(settings, monkeypatch, check=check)
    args = {"number": "8", "sha": "a" * 40}
    permit(r, task, "github_merge_pr", args)
    with pytest.raises(ToolError):
        r.execute("github_merge_pr", args, task_id=task, call_id="check")
    assert not any(c[0] == "PUT" for c in calls)


@pytest.mark.parametrize(
    "changes",
    [
        [{"filename": ".github/workflows/steal.yml"}],
        [{"filename": "new.py", "previous_filename": ".github/workflows/deploy.yml"}],
    ],
)
def test_merge_cannot_change_workflows(settings, monkeypatch, changes):
    r, task, calls = setup(settings, monkeypatch, changes)
    args = {"number": "8", "sha": "a" * 40}
    permit(r, task, "github_merge_pr", args)
    with pytest.raises(ToolError):
        r.execute("github_merge_pr", args, task_id=task, call_id="check")
    assert not any(c[0] == "PUT" for c in calls)


def test_exact_merge_approval_and_receipt(settings, monkeypatch):
    r, task, calls = setup(settings, monkeypatch)
    args = {"number": "8", "sha": "a" * 40}
    with pytest.raises(ToolError):
        r.execute("github_merge_pr", args, task_id=task, call_id="check")
    permit(r, task, "github_merge_pr", args)
    assert r.execute("github_merge_pr", args, task_id=task, call_id="check") == {
        "merged": True,
        "sha": "d" * 40,
    }
    r.execute("github_merge_pr", args, task_id=task, call_id="check")
    assert sum(c[0] == "PUT" for c in calls) == 1


def test_workflow_selection_stale_head_and_results(settings, monkeypatch):
    r, task, calls = setup(settings, monkeypatch)
    args = {"workflow": "steal.yml", "sha": "a" * 40}
    permit(r, task, "github_dispatch_workflow", args)
    with pytest.raises(ToolError):
        r.execute("github_dispatch_workflow", args, task_id=task, call_id="check")
    args = {"workflow": "deploy.yml", "sha": "b" * 40}
    permit(r, task, "github_dispatch_workflow", args, call_id="stale")
    with pytest.raises(ToolError):
        r.execute("github_dispatch_workflow", args, task_id=task, call_id="stale")
    assert not any(c[0] == "POST" for c in calls)
    assert (
        r.execute(
            "github_workflow_status",
            {"workflow": "deploy.yml", "sha": "a" * 40},
            task_id=task,
            call_id="read",
        )["runs"][0]["id"]
        == 42
    )


def test_app_tokens_are_repository_scoped_short_lived_and_revoked(settings, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from types import SimpleNamespace

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings.github_app_id, settings.github_installation_id = "1", "2"
    settings.github_app_private_key = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    settings.github_repo = "owner/repo"
    r, task = runnable(settings)
    requests, revocations = [], []

    def request(method, url, headers=None, payload=None):
        requests.append((method, url, payload))
        if "/access_tokens" in url:
            return {"token": "leased-test-token", "expires_at": "2099-01-01T00:00:00Z"}
        return []

    monkeypatch.setattr(r, "_request", request)
    monkeypatch.setattr(
        "agent4good.tools.httpx.delete",
        lambda *a, **kw: (
            revocations.append(kw["headers"]["Authorization"]) or SimpleNamespace(status_code=204)
        ),
    )
    assert r.execute("github_list_issues", {}, task_id=task, call_id="check") == []
    assert requests[0][2] == {"repositories": ["repo"], "permissions": {"contents": "read", "issues": "read"}}
    assert revocations == ["Bearer leased-test-token"]
    assert "leased-test-token" not in str(r.db.all("SELECT * FROM events"))
    assert "leased-test-token" not in str(r.db.all("SELECT * FROM tool_runs"))
