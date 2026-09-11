import httpx


class ResponsesProvider:
    def __init__(self, settings):
        self.settings = settings

    def respond(self, instructions, items, tools):
        if not self.settings.openai_api_key:
            raise RuntimeError("Configure A4G_OPENAI_API_KEY before running agents.")
        with httpx.Client(timeout=httpx.Timeout(90, connect=10), follow_redirects=False) as client:
            response = client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                json={
                    "model": self.settings.model,
                    "instructions": instructions,
                    "input": items,
                    "tools": tools,
                    "max_output_tokens": self.settings.max_output_tokens,
                    "parallel_tool_calls": False,
                    "store": False,
                    "include": ["reasoning.encrypted_content"],
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Model request failed (HTTP {response.status_code}). Check provider access and quota."
            )
        data = response.json()
        if data.get("status") not in {None, "completed"}:
            raise RuntimeError(
                "Model response did not complete. Reduce task scope or increase the output limit."
            )
        output = data.get("output", [])
        text = "\n".join(
            part.get("text", "")
            for item in output
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        return {"output": output, "output_text": text, "usage": data.get("usage", {})}
