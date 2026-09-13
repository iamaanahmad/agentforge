import json
import httpx
import pytest
from pydantic import ValidationError
from agent4good.config import Settings
from agent4good.db import Database
from agent4good.engine import Engine
from agent4good.model_config import ModelProfile, WORK_TYPES, resolve_profile
from agent4good.model_router import ModelRouter
from agent4good.provider import AnthropicProvider, ResponsesProvider, ProviderError
from agent4good.credentials import TASK_CONTEXT
from agent4good.tools import ToolRegistry


def mock_http(monkeypatch, responses):
    requests = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            value = responses.pop(0)
            if callable(value):
                value = value()
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr(httpx, "Client", Client)
    return requests


def oa(text="Done"):
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
            ],
            "usage": {"input_tokens": 20, "output_tokens": 4},
        },
    )


def ant(content=None, stop="end_turn"):
    return httpx.Response(
        200,
        json={
            "stop_reason": stop,
            "content": content or [{"type": "text", "text": "Done"}],
            "usage": {"input_tokens": 20, "output_tokens": 4},
        },
    )


def setup(settings, work="planning"):
    db = Database(settings.data_dir / "routing.sqlite")
    task = db.create_task("Routing test", "Complete task", "strategist", True, work_type=work)
    registry = ToolRegistry(settings, db)
    engine = Engine(db, settings, registry=registry)
    assert engine.claim() == task
    return db, task, engine, ModelRouter(settings, registry.credentials, db)


def anthropic(settings):
    settings.anthropic_api_key = "test-anthropic-key"
    profile = ModelProfile(provider="anthropic", model="claude-sonnet-4-20250514")
    settings.model_profiles = {"claude": profile}
    settings.model_routes = dict.fromkeys(WORK_TYPES, "claude")
    return profile


@pytest.mark.parametrize("work", WORK_TYPES)
def test_each_work_type_uses_independent_model(settings, monkeypatch, work):
    settings.openai_api_key = "test-openai-key"
    settings.model_profiles = {w: ModelProfile(model="test-" + w) for w in WORK_TYPES}
    settings.model_routes = {w: w for w in WORK_TYPES}
    requests = mock_http(monkeypatch, [oa()])
    db, task, engine, router = setup(settings, work)
    engine.run(task)
    assert db.task(task)["status"] == "done"
    assert requests[0][1]["json"]["model"] == "test-" + work
    assert router.readiness(work)["verification"] == "live_call_recorded"
    assert (
        db.one("SELECT usage FROM model_calls")["usage"]
        == '{"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}'
    )


def test_anthropic_engine_tool_roundtrip(settings, monkeypatch):
    anthropic(settings)
    calls = [{"type": "tool_use", "id": "toolu_test", "name": "memory_read", "input": {"key": "notes"}}]
    requests = mock_http(monkeypatch, [ant(calls, "tool_use"), ant()])
    db, task, engine, _ = setup(settings)
    engine.run(task)
    assert db.task(task)["status"] == "done"
    assert len(requests) == 2
    assert all(x[0] == "https://api.anthropic.com/v1/messages" for x in requests)
    body = requests[1][1]["json"]
    assert body["messages"][-2]["content"][0]["id"] == "toolu_test"
    assert body["messages"][-1]["content"][0]["tool_use_id"] == "toolu_test"
    assert requests[0][1]["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in requests[0][1]["headers"]
    assert "input_schema" in body["tools"][0]
    assert db.one("SELECT status FROM tool_runs")["status"] == "done"


@pytest.mark.parametrize(
    "status,retryable",
    [(401, False), (403, False), (400, False), (302, False), (429, True), (503, True), (529, True)],
)
def test_safe_http_errors(settings, monkeypatch, status, retryable):
    settings.openai_api_key = "test-key"
    mock_http(monkeypatch, [httpx.Response(status, text="sensitive-provider-body")])
    with pytest.raises(ProviderError) as error:
        ResponsesProvider(settings).respond("Task", [], [])
    assert error.value.retryable is retryable
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "bad",
    [
        httpx.Response(200, text="invalid"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"status": "incomplete", "output": []}),
        httpx.Response(
            200,
            json={
                "output": [
                    {"type": "function_call", "call_id": "x", "name": "memory_read", "arguments": "[]"}
                ]
            },
        ),
        httpx.Response(200, json={"output": [{"type": "web_search_call"}]}),
    ],
)
def test_protocol_rejects_malformed_or_partial_results(settings, monkeypatch, bad):
    settings.openai_api_key = "test-key"
    mock_http(monkeypatch, [bad])
    with pytest.raises(ProviderError):
        ResponsesProvider(settings).respond("Task", [], [])


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "pause_turn"])
def test_anthropic_partial_tools_never_execute(settings, monkeypatch, stop):
    anthropic(settings)
    mock_http(
        monkeypatch,
        [ant([{"type": "tool_use", "id": "x", "name": "memory_read", "input": {"key": "x"}}], stop)],
    )
    db, task, engine, _ = setup(settings)
    engine.run(task)
    assert db.task(task)["status"] == "failed"
    assert db.all("SELECT * FROM tool_runs") == []


def test_timeout_retries_bounded_and_no_fallback(settings, monkeypatch):
    anthropic(settings)
    settings.openai_api_key = "another-key"
    requests = mock_http(monkeypatch, [httpx.ReadTimeout("secret header")] * 3)
    db, task, engine, _ = setup(settings)
    engine.run(task)
    assert db.task(task)["status"] == "failed"
    assert "retry limit" in db.task(task)["error"]
    assert len(requests) == 3
    assert all(x[0].startswith("https://api.anthropic.com/") for x in requests)
    assert len(db.all("SELECT * FROM model_calls")) == 3
    assert "secret header" not in json.dumps(db.all("SELECT * FROM events"))


def test_cancel_before_network_and_during_network(settings, monkeypatch):
    settings.openai_api_key = "test-key"
    requests = mock_http(monkeypatch, [oa()])
    with pytest.raises(ProviderError, match="cancelled"):
        ResponsesProvider(settings).respond("Task", [], [], cancelled=lambda: True)
    assert not requests
    db, task, engine, _ = setup(settings)

    def cancel():
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task,))
        return oa()

    requests = mock_http(monkeypatch, [cancel])
    engine.run(task)
    assert db.task(task)["status"] == "cancelled"
    assert db.task(task)["result"] == ""
    assert db.all("SELECT * FROM tool_runs") == []


def test_route_pinned_across_restart_after_write(settings, monkeypatch):
    settings.openai_api_key = "test-key"
    requests = mock_http(monkeypatch, [oa()])
    db, task, engine, router = setup(settings)
    first = router.pin(task, [{"role": "user", "content": "Task"}])
    # Any attempted route change must not affect this task, including after completed writes.
    anthropic(settings)
    restored = ModelRouter(
        settings, engine.registry.credentials, Database(settings.data_dir / "routing.sqlite")
    )
    assert restored.pin(task, [{"type": "function_call_output"}]) == first
    engine.run(task)
    assert requests[0][0] == "https://api.openai.com/v1/responses"


def test_legacy_transcript_cannot_switch_providers(settings):
    anthropic(settings)
    _, task, _, router = setup(settings)
    with pytest.raises(ProviderError, match="Legacy transcript"):
        router.pin(task, [{"type": "function_call_output"}])


def test_token_and_cost_limits_survive_restart(settings, monkeypatch):
    settings.openai_api_key = "test-key"
    settings.max_task_model_reserved_tokens = 13000
    requests = mock_http(monkeypatch, [oa()])
    db, task, engine, router = setup(settings)
    token = TASK_CONTEXT.set(task)
    try:
        router.respond_task(task, "Task", [], [])
        restored = ModelRouter(
            settings, engine.registry.credentials, Database(settings.data_dir / "routing.sqlite")
        )
        with pytest.raises(ProviderError, match="token reservation"):
            restored.respond_task(task, "Task", [], [])
    finally:
        TASK_CONTEXT.reset(token)
    assert len(requests) == 1
    assert db.one("SELECT reserved_tokens FROM model_calls")["reserved_tokens"] > 12000


def test_cost_cap_fails_without_prices_and_before_charge(settings, monkeypatch):
    settings.openai_api_key = "test-key"
    settings.max_task_model_cost_usd = 0.001
    requests = mock_http(monkeypatch, [])
    db, task, _, router = setup(settings)
    token = TASK_CONTEXT.set(task)
    try:
        with pytest.raises(ProviderError, match="owner-configured"):
            router.respond_task(task, "Task", [], [])
        db.execute("DELETE FROM model_routes")
        settings.model_profiles = {
            "priced": ModelProfile(model="test", input_usd_per_million=10, output_usd_per_million=30)
        }
        settings.model_routes = {"planning": "priced"}
        with pytest.raises(ProviderError, match="cost limit"):
            router.respond_task(task, "Task", [], [])
    finally:
        TASK_CONTEXT.reset(token)
    assert not requests
    assert not db.all("SELECT * FROM model_calls")


def test_capabilities_and_schema_checked_before_network(settings, monkeypatch):
    settings.openai_api_key = "test-key"
    requests = mock_http(monkeypatch, [])
    profile = ModelProfile(model="text-only", tools=False)
    provider = ResponsesProvider(settings, profile=profile)
    with pytest.raises(ProviderError, match="tools"):
        provider.respond("Task", [], [{"type": "function"}])
    with pytest.raises(ProviderError, match="structured"):
        provider.respond("Task", [], [], schema={"type": "object"})
    with pytest.raises(ValidationError):
        ModelProfile(provider="anthropic", model="claude-test", structured_output=True)
    with pytest.raises(ProviderError, match="input limit"):
        provider.respond("X" * 400001, [], [])
    assert not requests


def test_structured_output_validates_locally(settings, monkeypatch):
    settings.openai_api_key = "test-key"
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    requests = mock_http(monkeypatch, [oa('{"ok":true}'), oa('{"ok":"wrong"}')])
    provider = ResponsesProvider(settings, profile=ModelProfile(model="test", structured_output=True))
    assert provider.respond("Task", [], [], schema=schema)["structured_output"] == {"ok": True}
    assert requests[0][1]["json"]["text"]["format"]["schema"] == schema
    with pytest.raises(ProviderError, match="schema"):
        provider.respond("Task", [], [], schema=schema)


def test_missing_anthropic_credential_and_opaque_reasoning(settings, monkeypatch):
    profile = anthropic(settings)
    settings.anthropic_api_key = ""
    requests = mock_http(monkeypatch, [])
    _, _, _, router = setup(settings)
    assert router.readiness("planning")["available"] is False
    assert router.readiness("planning")["verification"] == "not_live_verified"
    with pytest.raises(ProviderError, match="Configure A4G_ANTHROPIC"):
        AnthropicProvider(settings, profile=profile).respond(
            "Task", [{"role": "user", "content": "Task"}], []
        )
    with pytest.raises(ProviderError, match="transferred"):
        AnthropicProvider(settings, profile=profile).respond(
            "Task", [{"type": "reasoning", "encrypted_content": "opaque"}], []
        )
    assert not requests


def test_config_rejects_unknown_profiles_work_types_and_endpoints(settings):
    for kwargs in [
        {"model_routes": {"unknown": "x"}},
        {"model_routes": {"planning": "missing"}},
        {"model_profiles": {"x": {"provider": "arbitrary", "model": "test"}}},
        {"model_profiles": {"x": {"model": "test", "endpoint": "https://evil.test"}}},
    ]:
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                **{
                    **settings.model_dump(),
                    "admin_password": settings.admin_password,
                    "session_secret": settings.session_secret,
                    **kwargs,
                },
            )
    assert resolve_profile(settings, "coding").model == settings.model


def test_api_uses_selected_provider_and_explicit_work_type(settings, monkeypatch):
    from agent4good.app import create_app
    from fastapi.testclient import TestClient

    anthropic(settings)
    with TestClient(create_app(settings)) as client:
        login = client.post("/api/login", json={"password": settings.admin_password})
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        response = client.post(
            "/api/tasks", json={"title": "Test", "prompt": "Task", "start": True, "work_type": "browsing"}
        )
        assert response.status_code == 201
        assert response.json()["work_type"] == "browsing"
        routes = client.get("/api/readiness").json()["model_routes"]
        assert set(routes) == set(WORK_TYPES)
        assert all(r["provider"] == "anthropic" for r in routes.values())
        assert "test-anthropic-key" not in json.dumps(routes)


def test_approved_external_write_keeps_route_and_receipt_after_model_failure(settings, monkeypatch):
    anthropic(settings)
    settings.openai_api_key = "unused-fallback-key"
    settings.resend_api_key = "test-mail-key"
    settings.mail_from = "owner@example.com"
    args = {"to": "test@example.com", "subject": "Test", "body": "Exact approved text"}
    requests = mock_http(
        monkeypatch,
        [
            ant([{"type": "tool_use", "id": "mail_test", "name": "send_email", "input": args}], "tool_use"),
            httpx.ReadTimeout("hidden"),
            httpx.ReadTimeout("hidden"),
            httpx.ReadTimeout("hidden"),
        ],
    )
    db, task, engine, _ = setup(settings)
    sent = []
    engine.registry._request = lambda *args: sent.append(args[3]) or {"id": "test_receipt"}
    engine.run(task)
    assert db.task(task)["status"] == "waiting_approval"
    assert sent == []
    settings.model_routes = {}  # New tasks would use OpenAI, but this task must not.
    db.execute("UPDATE approvals SET status='approved' WHERE task_id=?", (task,))
    db.execute("UPDATE tasks SET status='queued' WHERE id=?", (task,))
    restored = Engine(Database(settings.data_dir / "routing.sqlite"), settings, registry=engine.registry)
    assert restored.claim() == task
    restored.run(task)
    assert len(sent) == 1
    assert db.one("SELECT status FROM tool_runs WHERE tool='send_email'")["status"] == "done"
    assert db.task(task)["status"] == "failed"
    assert len(requests) == 4
    assert all(url == "https://api.anthropic.com/v1/messages" for url, _ in requests)
    restored.run(task)
    assert len(sent) == 1
