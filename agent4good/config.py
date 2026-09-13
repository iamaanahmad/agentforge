from pathlib import Path
import os
from typing import Literal
from pydantic import Field, model_validator
from .model_config import ModelProfile, WorkType
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="A4G_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    environment: str = Field("local", min_length=1, max_length=160)
    tenant_id: str = Field("default", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    isolation_mode: Literal["single_owner"] = "single_owner"
    credential_key_file: Path | None = None
    data_dir: Path = Path("data")
    database_url: str = Field("", repr=False, exclude=True)
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = Field("", repr=False, exclude=True)
    s3_secret_key: str = Field("", repr=False, exclude=True)
    worker_lease_seconds: int = Field(60, ge=15, le=300)
    max_concurrent_runs: int = Field(4, ge=1, le=100)
    max_queued_tasks: int = Field(1000, ge=1, le=100000)
    admin_password: str = Field("", repr=False, exclude=True)
    session_secret: str = Field("", repr=False, exclude=True)
    secure_cookies: bool = True
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]
    public_origin: str = "http://localhost:8000"
    openai_api_key: str = Field("", repr=False, exclude=True)
    anthropic_api_key: str = Field("", repr=False, exclude=True)
    model: str = "gpt-5.4-mini"
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    model_routes: dict[WorkType, str] = Field(default_factory=dict)
    max_task_model_reserved_tokens: int = Field(2000000, ge=1024, le=50000000)
    max_task_model_cost_usd: float | None = Field(None, gt=0, le=10000, allow_inf_nan=False)
    max_output_tokens: int = Field(4096, ge=256, le=16000)
    max_steps: int = Field(12, ge=1, le=30)
    max_recoveries: int = Field(3, ge=0, le=20)
    max_model_retries: int = Field(2, ge=0, le=5)
    task_timeout_seconds: int = Field(3600, ge=60, le=86400)
    max_daily_runs: int = Field(30, ge=1, le=1000)
    github_token: str = Field("", repr=False, exclude=True)
    github_repo: str = ""
    sandbox_socket: str = ""
    github_merge_checks: list[str] = []
    github_deploy_workflows: list[str] = []
    resend_api_key: str = Field("", repr=False, exclude=True)
    mail_from: str = ""
    search_api_key: str = Field("", repr=False, exclude=True)
    allowed_read_hosts: list[str] = []
    worker_poll_seconds: float = Field(2, ge=0.2, le=60)

    def __init__(self, **values):
        # Docker/Kubernetes secret mounts, with explicit constructor values winning in tests.
        for name in ("database_url", "s3_access_key", "s3_secret_key", "admin_password", "session_secret"):
            path = os.getenv("A4G_" + name.upper() + "_FILE")
            if path and name not in values:
                try:
                    value = Path(path).read_text().strip()
                    if not value or len(value) > 8192:
                        raise ValueError()
                    values[name] = value
                except (OSError, ValueError):
                    raise ValueError("Infrastructure secret file is missing or invalid") from None
        super().__init__(**values)

    @model_validator(mode="after")
    def secure_configuration(self):
        if self.database_url:
            if not self.database_url.startswith(("postgresql://", "postgres://")):
                raise ValueError("Database URL must use PostgreSQL")
            if not self.s3_bucket:
                raise ValueError("Distributed mode requires an object storage bucket")

        if len(self.admin_password) < 16 or len(self.session_secret) < 32:
            raise ValueError(
                "Set A4G_ADMIN_PASSWORD (16+ characters) and A4G_SESSION_SECRET (32+ characters)"
            )
        if self.secure_cookies and not self.public_origin.startswith("https://"):
            raise ValueError(
                "Production requires an HTTPS public origin; local development may disable secure cookies"
            )
        if any(name not in self.model_profiles for name in self.model_routes.values()):
            raise ValueError("Every model route must name a configured profile")
        return self
