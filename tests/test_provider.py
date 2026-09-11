import httpx
import pytest
from agent4good.provider import ResponsesProvider


def test_responses_protocol_and_stateless_reasoning(settings, monkeypatch):
    settings.openai_api_key = "test-key"
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {"type": "reasoning", "encrypted_content": "opaque"},
                        {"type": "message", "content": [{"type": "output_text", "text": "Result"}]},
                    ],
                    "usage": {"total_tokens": 12},
                },
            )

    monkeypatch.setattr(httpx, "Client", Client)
    result = ResponsesProvider(settings).respond("Instructions", [{"role": "user", "content": "Task"}], [])
    assert result["output_text"] == "Result"
    assert result["usage"]["total_tokens"] == 12
    assert requests[0][0] == "https://api.openai.com/v1/responses"
    body = requests[0][1]["json"]
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["parallel_tool_calls"] is False


def test_missing_key_never_contacts_provider(settings, monkeypatch):
    def fail(**kwargs):
        raise AssertionError("Must not make network calls")

    monkeypatch.setattr(httpx, "Client", fail)
    with pytest.raises(RuntimeError, match="Configure"):
        ResponsesProvider(settings).respond("Task", [], [])
