"""Owner-controlled model profiles. No URLs, secrets, or fallback are configurable here."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

WorkType = Literal["planning", "coding", "browsing", "research", "summarization", "verification"]
WORK_TYPES = ("planning", "coding", "browsing", "research", "summarization", "verification")
ROLE_WORK = {
    "product_engineer": "coding",
    "research_analyst": "research",
    "organic_growth_engineer": "research",
    "outreach_engineer": "research",
    "customer_support_engineer": "summarization",
    "product_analyst": "verification",
}


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    provider: Literal["openai", "anthropic"] = "openai"
    model: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$")
    # Explicit owner declarations, not claims of provider/account verification.
    tools: bool = True
    structured_output: bool = False
    max_output_tokens: int = Field(4096, ge=256, le=16000)
    max_input_bytes: int = Field(400000, ge=1024, le=1000000)
    timeout_seconds: int = Field(90, ge=1, le=120)
    input_usd_per_million: float | None = Field(None, ge=0, le=10000, allow_inf_nan=False)
    output_usd_per_million: float | None = Field(None, ge=0, le=10000, allow_inf_nan=False)

    @model_validator(mode="after")
    def capabilities(self):
        if self.provider == "anthropic" and self.structured_output:
            raise ValueError("This Anthropic adapter does not implement schema-constrained final output")
        return self


def resolve_profile(settings, work_type):
    if work_type not in WORK_TYPES:
        raise ValueError("Unsupported work type")
    name = settings.model_routes.get(work_type)
    if name:
        return settings.model_profiles[name]
    return ModelProfile(model=settings.model, max_output_tokens=settings.max_output_tokens)


def task_work(task):
    return task.get("work_type") or ROLE_WORK.get(task["agent"], "planning")
