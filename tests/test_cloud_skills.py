import json
import httpx
import pytest
from pydantic import ValidationError
from agent4good.provider import BedrockProvider, VertexProvider, ProviderError
from agent4good.model_config import ModelProfile
from agent4good.skills import CATALOG, read_skill
from agent4good.tools import ToolRegistry, SPECS, ToolError
from agent4good.db import Database


TOOL = {
    "type": "function",
    "name": "skill_read",
    "description": "Read skill",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
}


def test_bedrock_roundtrip_and_no_ambient_identity(settings, monkeypatch):
    import boto3

    settings.bedrock_credentials = json.dumps(
        {"access_key_id": "test-access", "secret_access_key": "test-secret"}
    )
    profile = ModelProfile(provider="bedrock", model="example-model", region="us-east-1")
    sent = []
    turn = {
        "role": "assistant",
        "content": [
            {"reasoningContent": {"reasoningText": {"text": "opaque", "signature": "saved-signature"}}},
            {"toolUse": {"toolUseId": "call1", "name": "skill_read", "input": {"name": "dataforseo"}}},
        ],
    }

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def converse(self, **body):
            sent.append(body)
            return {
                "stopReason": "tool_use",
                "output": {"message": turn},
                "usage": {"inputTokens": 7, "outputTokens": 4},
            }

    def client(service, **kwargs):
        assert service == "bedrock-runtime"
        assert kwargs["aws_access_key_id"] == "test-access"
        assert kwargs["config"].retries["total_max_attempts"] == 1
        assert kwargs["config"].proxies == {}
        assert kwargs["endpoint_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com"
        return Client()

    monkeypatch.setattr(boto3, "client", client)
    adapter = BedrockProvider(settings, profile=profile)
    result = adapter.respond("system", [{"role": "user", "content": "Research"}], [TOOL])
    items = (
        [{"role": "user", "content": "Research"}]
        + result["output"]
        + [{"type": "function_call_output", "call_id": "call1", "output": "safe result"}]
    )
    adapter.respond("system", items, [TOOL])
    assert sent[1]["messages"][1] == turn
    assert sent[1]["messages"][2]["content"][0]["toolResult"]["toolUseId"] == "call1"
    assert result["usage"]["total_tokens"] == 11


def test_vertex_preserves_signature_and_tool_result(settings, monkeypatch):
    profile = ModelProfile(provider="vertex", model="gemini-test", project="owner-project")
    adapter = VertexProvider(settings, profile=profile)
    monkeypatch.setattr(adapter, "token", lambda: "test-token")
    sent = []
    part = {
        "functionCall": {"id": "vertex-call-1", "name": "skill_read", "args": {"name": "dataforseo"}},
        "thoughtSignature": "opaque-signature",
    }

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            assert url.startswith("https://aiplatform.googleapis.com/v1/projects/owner-project/")
            sent.append(kwargs["json"])
            return httpx.Response(
                200,
                json={
                    "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [part]}}],
                    "usageMetadata": {
                        "promptTokenCount": 4,
                        "candidatesTokenCount": 2,
                        "thoughtsTokenCount": 3,
                    },
                },
            )

    monkeypatch.setattr(httpx, "Client", Client)
    items = [{"role": "user", "content": "Research"}]
    result = adapter.respond("system", items, [TOOL])
    call = next(i for i in result["output"] if i["type"] == "function_call")
    adapter.respond(
        "system",
        items
        + result["output"]
        + [{"type": "function_call_output", "call_id": call["call_id"], "output": "saved"}],
        [TOOL],
    )
    assert sent[1]["contents"][1]["parts"] == [part]
    assert sent[1]["contents"][2]["parts"][0]["functionResponse"]["name"] == "skill_read"
    assert sent[1]["contents"][2]["parts"][0]["functionResponse"]["id"] == "vertex-call-1"
    assert result["usage"]["output_tokens"] == 5


@pytest.mark.parametrize("provider", ["bedrock", "vertex"])
def test_cloud_cancel_before_credentials(settings, provider):
    profile = ModelProfile(provider=provider, model="example", project="owner-project")
    cls = BedrockProvider if provider == "bedrock" else VertexProvider
    with pytest.raises(ProviderError, match="cancelled"):
        cls(settings, profile=profile).respond("test", [], [], cancelled=lambda: True)


@pytest.mark.parametrize(
    "values",
    [
        {"provider": "vertex", "project": ""},
        {"provider": "vertex", "project": "owner/evil"},
        {"provider": "bedrock", "region": "evil.example.com"},
        {"provider": "vertex", "location": "../evil"},
        {"provider": "bedrock", "structured_output": True},
    ],
)
def test_invalid_cloud_profiles(values):
    with pytest.raises(ValidationError):
        ModelProfile(model="test", **values)


def test_vertex_rejects_untrusted_auth_endpoint(settings):
    settings.vertex_credentials = json.dumps(
        {"type": "service_account", "token_uri": "https://evil.example/token"}
    )
    with pytest.raises(ProviderError, match="service-account"):
        VertexProvider(
            settings, profile=ModelProfile(provider="vertex", model="test", project="owner-project")
        ).token()


def test_packaged_skills_are_bounded_and_versioned():
    assert len(CATALOG) == 10
    for name in CATALOG:
        result = read_skill(name)
        assert len(result["version"]) == 64
        assert result["instructions"]
    with pytest.raises(ValueError):
        read_skill("../../.env")


def test_paid_search_requires_approval_and_secret_redaction(settings):
    settings.dataforseo_credentials = json.dumps(
        {"login": "owner@example.com", "password": "secret-password"}
    )
    registry = ToolRegistry(settings, Database(settings.data_dir / "test.sqlite"))
    args = {"keywords": '["agent software"]', "location_code": "2840", "language_code": "en"}
    assert registry.requires_approval("dataforseo_search_volume", args, "high")
    assert SPECS["dataforseo_search_volume"].action_class == "FINANCIAL"
    assert registry.credentials.redact("secret-password") == "[redacted]"
    with pytest.raises(ToolError):
        registry.requires_approval("skill_read", {"name": "../../.env"}, "high")


def test_paid_search_one_receipt_no_duplicate_charge(settings, monkeypatch):
    from test_tools import runnable, permit

    settings.dataforseo_credentials = json.dumps(
        {"login": "owner@example.com", "password": "secret-password"}
    )
    registry, task = runnable(settings)
    args = {"keywords": '["agent software"]', "location_code": "2840", "language_code": "en"}
    sent = []

    def request(method, url, headers, payload):
        sent.append(payload)
        assert method == "POST" and url.startswith("https://api.dataforseo.com/v3/")
        return {
            "status_code": 20000,
            "tasks": [
                {"status_code": 20000, "result": [{"keyword": "agent software", "search_volume": None}]}
            ],
        }

    monkeypatch.setattr(registry, "_request", request)
    with pytest.raises(ToolError):
        registry.execute("dataforseo_search_volume", args, task_id=task, call_id="check")
    assert not sent
    from agent4good.policy import PolicyDocument

    registry.policy.replace(
        PolicyDocument(tool_costs_microusd={"dataforseo_search_volume": 100000}), expected_revision=1
    )
    permit(registry, task, "dataforseo_search_volume", args)
    result = registry.execute("dataforseo_search_volume", args, task_id=task, call_id="check")
    assert result["results"][0]["search_volume"] is None
    assert registry.execute("dataforseo_search_volume", args, task_id=task, call_id="check") == result
    assert len(sent) == 1


def test_skill_tool_through_registry(settings):
    from test_tools import runnable

    registry, task = runnable(settings)
    result = registry.execute("skill_read", {"name": "dataforseo"}, task_id=task, call_id="skill")
    assert result["name"] == "dataforseo"
    assert "paid request" in result["instructions"]


@pytest.mark.parametrize("status", ["MAX_TOKENS", "SAFETY"])
def test_vertex_incomplete_never_returns_actions(settings, monkeypatch, status):
    adapter = VertexProvider(
        settings, profile=ModelProfile(provider="vertex", model="test", project="owner-project")
    )
    monkeypatch.setattr(adapter, "token", lambda: "test-token")
    from test_model_routing import mock_http

    mock_http(
        monkeypatch,
        [
            httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": status,
                            "content": {"parts": [{"functionCall": {"name": "send_email", "args": {}}}]},
                        }
                    ]
                },
            )
        ],
    )
    with pytest.raises(ProviderError, match="incomplete"):
        adapter.respond("system", [{"role": "user", "content": "test"}], [TOOL])


@pytest.mark.parametrize("provider", ["bedrock", "vertex"])
def test_cloud_engine_executes_skill_and_saves_result(settings, monkeypatch, provider):
    from agent4good.model_config import WORK_TYPES
    from test_model_routing import setup, mock_http

    profile = ModelProfile(provider=provider, model="test-model", project="owner-project")
    settings.model_profiles = {"cloud": profile}
    settings.model_routes = dict.fromkeys(WORK_TYPES, "cloud")
    sent = []
    if provider == "vertex":
        settings.vertex_credentials = '{"type":"service_account"}'
        monkeypatch.setattr(VertexProvider, "token", lambda self: "test-token")
        turns = [
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": "skill_read", "args": {"name": "dataforseo"}},
                        "thoughtSignature": "opaque",
                    }
                ],
            },
            {"role": "model", "parts": [{"text": "Read the skill. No paid query ran."}]},
        ]
        sent = mock_http(
            monkeypatch,
            [
                httpx.Response(
                    200,
                    json={
                        "candidates": [{"finishReason": "STOP", "content": t}],
                        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4},
                    },
                )
                for t in turns
            ],
        )
    else:
        import boto3

        settings.bedrock_credentials = '{"access_key_id":"test-access","secret_access_key":"test-secret"}'
        turns = [
            {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "c1", "name": "skill_read", "input": {"name": "dataforseo"}}}
                ],
            },
            {"role": "assistant", "content": [{"text": "Read the skill. No paid query ran."}]},
        ]

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def converse(self, **kwargs):
                sent.append(kwargs)
                return {
                    "output": {"message": turns.pop(0)},
                    "stopReason": "tool_use" if len(sent) == 1 else "end_turn",
                    "usage": {"inputTokens": 10, "outputTokens": 4},
                }

        monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: Client())
    db, task, engine, router = setup(settings)
    engine.run(task)
    assert db.task(task)["status"] == "done"
    assert len(sent) == 2
    assert db.one("SELECT status FROM tool_runs")["status"] == "done"
    assert len(db.all("SELECT * FROM model_calls")) == 2
    assert router.readiness("planning")["configured"]
