from pathlib import Path
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="A4G_", env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    admin_password: str = ""
    session_secret: str = ""
    secure_cookies: bool = True
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]
    public_origin: str = "http://localhost:8000"
    openai_api_key: str = ""
    model: str = "gpt-5.4-mini"
    max_output_tokens: int = Field(4096, ge=256, le=16000)
    max_steps: int = Field(12, ge=1, le=30)
    max_daily_runs: int = Field(30, ge=1, le=1000)
    github_token: str = ""
    github_repo: str = ""
    resend_api_key: str = ""
    mail_from: str = ""
    search_api_key: str = ""
    allowed_read_hosts: list[str] = []
    worker_poll_seconds: float = Field(2, ge=0.2, le=60)

    @model_validator(mode="after")
    def secure_configuration(self):
        if len(self.admin_password) < 16 or len(self.session_secret) < 32:
            raise ValueError(
                "Set A4G_ADMIN_PASSWORD (16+ characters) and A4G_SESSION_SECRET (32+ characters)"
            )
        if self.secure_cookies and not self.public_origin.startswith("https://"):
            raise ValueError(
                "Production requires an HTTPS public origin; local development may disable secure cookies"
            )
        return self
