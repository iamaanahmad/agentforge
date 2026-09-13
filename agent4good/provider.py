"""Stateless provider adapters over a shared text/tool transcript.

Only client-side tools are allowed. Providers never execute external actions.
Errors carry safe categories, never provider bodies or request headers.
"""

import json
from typing import Protocol
import httpx
from jsonschema import Draft202012Validator, ValidationError, SchemaError
from .model_config import ModelProfile


class ProviderError(RuntimeError):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


class ModelProvider(Protocol):
    def respond(self, instructions, items, tools, *, schema=None, cancelled=None) -> dict: ...


def check_cancelled(cancelled):
    if cancelled and cancelled():
        raise ProviderError("cancelled", "Model request cancelled; no response actions will run")


def normalize(output, usage, schema=None):
    if not isinstance(output, list) or not isinstance(usage, dict):
        raise ProviderError("protocol", "Provider returned invalid output")
    text, calls = [], set()
    for item in output:
        if not isinstance(item, dict):
            raise ProviderError("protocol", "Provider returned invalid output")
        kind = item.get("type")
        if kind == "message":
            content = item.get("content")
            if not isinstance(content, list):
                raise ProviderError("protocol", "Provider returned invalid message")
            for part in content:
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "output_text"
                    or not isinstance(part.get("text"), str)
                ):
                    raise ProviderError("protocol", "Provider returned unsupported content or a refusal")
                text.append(part["text"])
        elif kind == "function_call":
            call_id, name = item.get("call_id"), item.get("name")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in calls
                or not isinstance(name, str)
                or not name
            ):
                raise ProviderError("protocol", "Provider returned invalid tool identity")
            try:
                if not isinstance(json.loads(item["arguments"]), dict):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ProviderError("protocol", "Provider returned invalid tool arguments") from None
            calls.add(call_id)
        elif kind != "reasoning":
            raise ProviderError("protocol", "Provider returned an unsupported output type")
    if len(calls) > 10:
        raise ProviderError("protocol", "Provider exceeded the per-response tool limit")
    result = {"output": output, "output_text": "\n".join(text), "usage": dict(usage)}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        if key in usage and (type(usage[key]) is not int or usage[key] < 0):
            raise ProviderError("protocol", "Provider returned invalid token usage")
    if "total_tokens" not in usage and "input_tokens" in usage and "output_tokens" in usage:
        result["usage"]["total_tokens"] = sum(
            usage.get(k, 0)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        )
    if schema is not None and not calls:
        try:
            parsed = json.loads(result["output_text"])
            Draft202012Validator(schema).validate(parsed)
        except (ValueError, ValidationError):
            raise ProviderError("schema", "Provider final output did not match the required schema") from None
        result["structured_output"] = parsed
    return result


class HTTPProvider:
    credential_name = ""
    endpoint = ""

    def __init__(self, settings, credentials=None, profile=None):
        self.settings, self.credentials = settings, credentials
        self.profile = profile or ModelProfile(
            model=settings.model, max_output_tokens=settings.max_output_tokens
        )

    def headers(self, key):
        return {"Authorization": f"Bearer {key}"}

    def post(self, body, cancelled):
        check_cancelled(cancelled)
        key = (
            self.credentials.get(self.credential_name, "model")
            if self.credentials
            else getattr(self.settings, self.credential_name)
        )
        if not key:
            raise ProviderError(
                "unavailable", "Configure A4G_" + self.credential_name.upper() + " before running agents."
            )
        try:
            with httpx.Client(
                timeout=httpx.Timeout(
                    self.profile.timeout_seconds, connect=min(10, self.profile.timeout_seconds)
                ),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(self.endpoint, headers=self.headers(key), json=body)
        except httpx.TransportError:
            raise ProviderError("transport", "Model transport failed or timed out", retryable=True) from None
        check_cancelled(cancelled)
        if response.status_code != 200:
            status = response.status_code
            raise ProviderError(
                "http",
                f"Model request failed (HTTP {status}). Check provider access and quota.",
                retryable=status in {408, 429, 500, 502, 503, 504, 529},
            )
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except ValueError:
            raise ProviderError("protocol", "Provider returned invalid JSON") from None

    def validate(self, instructions, items, tools, schema):
        if tools and not self.profile.tools:
            raise ProviderError("capability", "Selected model profile does not support tools")
        if any(t.get("type") != "function" for t in tools):
            raise ProviderError("capability", "Only local function tools are supported")
        if schema is not None:
            if not self.profile.structured_output:
                raise ProviderError("capability", "Selected model profile does not support structured output")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError:
                raise ProviderError("schema", "Invalid output schema") from None
        if (
            len(json.dumps([instructions, items, tools, schema], ensure_ascii=False).encode())
            > self.profile.max_input_bytes
        ):
            raise ProviderError("budget", "Model input limit reached; start a smaller task")


class ResponsesProvider(HTTPProvider):
    endpoint = "https://api.openai.com/v1/responses"
    credential_name = "openai_api_key"

    def respond(self, instructions, items, tools, *, schema=None, cancelled=None):
        self.validate(instructions, items, tools, schema)
        body = {
            "model": self.profile.model,
            "instructions": instructions,
            "input": items,
            "tools": tools,
            "max_output_tokens": self.profile.max_output_tokens,
            "parallel_tool_calls": False,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if schema is not None:
            body["text"] = {
                "format": {"type": "json_schema", "name": "result", "strict": True, "schema": schema}
            }
        data = self.post(body, cancelled)
        if data.get("status") not in {None, "completed"}:
            raise ProviderError(
                "incomplete",
                "Model response did not complete. Reduce task scope or increase the output limit.",
            )
        return normalize(data.get("output", []), data.get("usage") or {}, schema)


class AnthropicProvider(HTTPProvider):
    endpoint = "https://api.anthropic.com/v1/messages"
    credential_name = "anthropic_api_key"

    def __init__(self, settings, credentials=None, profile=None):
        if profile is None or profile.provider != "anthropic":
            raise ProviderError("capability", "Choose an explicit Anthropic model profile")
        super().__init__(settings, credentials, profile)

    def headers(self, key):
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}

    def messages(self, items):
        messages = []
        for item in items:
            kind = item.get("type")
            if kind == "function_call":
                role, blocks = (
                    "assistant",
                    [
                        {
                            "type": "tool_use",
                            "id": item["call_id"],
                            "name": item["name"],
                            "input": json.loads(item["arguments"]),
                        }
                    ],
                )
            elif kind == "function_call_output":
                role, blocks = (
                    "user",
                    [{"type": "tool_result", "tool_use_id": item["call_id"], "content": item["output"]}],
                )
            elif kind == "message" or (kind is None and item.get("role") in {"user", "assistant"}):
                role = item.get("role", "assistant")
                content = item["content"]
                if isinstance(content, str):
                    blocks = [{"type": "text", "text": content}]
                else:
                    if any(p.get("type") not in {"input_text", "output_text"} for p in content):
                        raise ProviderError(
                            "capability", "Anthropic adapter accepts text and function calls only"
                        )
                    blocks = [{"type": "text", "text": p["text"]} for p in content]
            else:
                # Never discard opaque OpenAI reasoning during a provider switch.
                raise ProviderError("capability", "Transcript cannot be transferred to Anthropic")
            if role not in {"user", "assistant"}:
                raise ProviderError("capability", "Unsupported transcript role")
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
        return messages

    def respond(self, instructions, items, tools, *, schema=None, cancelled=None):
        self.validate(instructions, items, tools, schema)
        body = {
            "model": self.profile.model,
            "system": instructions,
            "messages": self.messages(items),
            "max_tokens": self.profile.max_output_tokens,
        }
        if tools:
            body["tools"] = [
                {"name": t["name"], "description": t.get("description", ""), "input_schema": t["parameters"]}
                for t in tools
            ]
            body["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        data = self.post(body, cancelled)
        if data.get("stop_reason") not in {"end_turn", "tool_use"}:
            raise ProviderError(
                "incomplete", "Anthropic response did not complete; no partial tools will run"
            )
        output = []
        content = data.get("content")
        if not isinstance(content, list):
            raise ProviderError("protocol", "Anthropic returned invalid content")
        for block in content:
            if not isinstance(block, dict):
                raise ProviderError("protocol", "Anthropic returned invalid content")
            if block.get("type") == "text":
                output.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": block.get("text")}],
                    }
                )
            elif block.get("type") == "tool_use":
                output.append(
                    {
                        "type": "function_call",
                        "call_id": block.get("id"),
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input")),
                    }
                )
            else:
                raise ProviderError("capability", "Anthropic returned unsupported content")
        has_calls = any(x["type"] == "function_call" for x in output)
        if has_calls != (data["stop_reason"] == "tool_use"):
            raise ProviderError("protocol", "Anthropic stop reason does not match its content")
        return normalize(output, data.get("usage") or {}, schema)


ADAPTERS = {"openai": ResponsesProvider, "anthropic": AnthropicProvider}
