import socket
import pytest
from pydantic import ValidationError
from agent4good.config import Settings
from agent4good.tools import ToolRegistry, ToolError, public_addresses, validate_arguments


def test_settings_refuse_unsafe_defaults():
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            admin_password="long-enough-password",
            session_secret="s" * 40,
            secure_cookies=True,
            public_origin="http://localhost",
        )


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "0.0.0.0", "192.168.0.1"]
)
def test_private_ssrf_blocked(monkeypatch, address):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
    )
    with pytest.raises(ToolError):
        public_addresses("configured.test")


def test_dns_mixed_public_private_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ToolError):
        public_addresses("configured.test")


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "https://user:pass@example.com", "https://example.com:444", "https://evil.test"],
)
def test_url_policy_before_network(settings, url):
    settings.allowed_read_hosts = ["example.com"]
    r = ToolRegistry(settings)
    with pytest.raises(ToolError):
        r.execute("web_fetch", {"url": url})


@pytest.mark.parametrize(
    "path",
    ["../secret", "/etc/passwd", "foo//bar", ".env", "src/.env.local", "key.pem", ".git/config", "foo\\bar"],
)
def test_repository_path_policy(path):
    with pytest.raises(ToolError):
        ToolRegistry._path(path)


def test_workflow_writes_and_main_branch_blocked():
    with pytest.raises(ToolError):
        ToolRegistry._path(".github/workflows/deploy.yml", True)
    for branch in ["main", "agent4good/../../main", "agent4good/feature/one"]:
        with pytest.raises(ToolError):
            ToolRegistry._branch(branch)


def test_all_external_writes_require_approval(settings):
    r = ToolRegistry(settings)
    for mode in ["manual", "supervised", "autonomous"]:
        assert r.requires_approval(
            "send_email", {"to": "a@example.com", "subject": "Hi", "body": "Text"}, mode
        )
        assert r.requires_approval("github_create_issue", {"title": "Bug", "body": "Details"}, mode)
    assert not r.requires_approval("memory_read", {"key": "product"}, "autonomous")


def test_unknown_extra_and_wrong_type_arguments_rejected():
    for name, args in [
        ("unknown", {}),
        ("memory_read", {"key": "x", "bypass": True}),
        ("memory_read", {"key": 1}),
    ]:
        with pytest.raises(ToolError):
            validate_arguments(name, args)


def test_no_credentials_means_no_external_tools(settings):
    assert ToolRegistry(settings).definitions() == []
    with pytest.raises(ToolError):
        ToolRegistry(settings).execute("github_list_issues", {})


def test_email_exact_payload(settings, monkeypatch):
    settings.resend_api_key = "test-key"
    settings.mail_from = "Owner <owner@example.com>"
    r = ToolRegistry(settings)
    requests = []
    monkeypatch.setattr(r, "_request", lambda *args: requests.append(args) or {"id": "receipt"})
    args = {"to": "person@example.com", "subject": "Subject", "body": "Exact body\nWith newline"}
    assert r.execute("send_email", args) == {"message_id": "receipt"}
    assert requests[0][3] == {
        "from": settings.mail_from,
        "to": [args["to"]],
        "subject": args["subject"],
        "text": args["body"],
    }
    with pytest.raises(ToolError):
        r.execute("send_email", {**args, "to": "a@example.com,b@example.com"})


def test_default_branch_write_fails_even_if_prefixed(settings, monkeypatch):
    settings.github_token = "test-key"
    settings.github_repo = "owner/repo"
    r = ToolRegistry(settings)
    monkeypatch.setattr(r, "_github", lambda *args: {"default_branch": "agent4good/main"})
    with pytest.raises(ToolError):
        r.execute(
            "github_write_file",
            {"branch": "agent4good/main", "path": "README.md", "content": "x", "message": "x", "sha": ""},
        )
