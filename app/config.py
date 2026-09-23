from datetime import datetime
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env", extra="ignore"
    )
    firebase_credentials_path: str | None = None
    firebase_credentials_json: SecretStr | None = None
    firebase_project_id: str = "rsvp-revamp"
    firebase_database_id: str = "(default)"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    event_id: str = Field(default="devfest-lebanon-2026", pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    event_name: str = "DevFest Lebanon 2026 — Be Part of It"
    registration_deadline: datetime = datetime.fromisoformat("2026-12-31T23:59:59+02:00")
    force_close_registration: bool = False
    submissions_per_hour: int = Field(default=20, ge=1, le=1000)
    invitation_ttl_hours: int = Field(default=48, ge=1, le=336)
    public_base_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:5173/rsvp-gdg/"
    firebase_web_api_key: str = ""
    firebase_auth_domain: str = "rsvp-revamp.firebaseapp.com"
    firebase_web_app_id: str = ""
    ticket_signing_key: SecretStr | None = None
    sheets_webhook_secret: SecretStr | None = None
    mail_provider: str = "disabled"
    mail_from: str = ""
    sendgrid_api_key: SecretStr | None = None
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = True
    google_sheet_id: str = ""
    google_sheet_tab: str = "Attendees"

    @field_validator("ticket_signing_key", "sheets_webhook_secret")
    @classmethod
    def strong_secret(cls, value):
        if value and len(value.get_secret_value()) < 32:
            raise ValueError("Signing secrets must contain at least 32 characters")
        return value

    @field_validator("mail_provider")
    @classmethod
    def valid_mail_provider(cls, value):
        if value not in {"disabled", "sendgrid", "smtp"}:
            raise ValueError("Choose disabled, sendgrid, or smtp")
        return value

    vip_code_hashes: list[str] = []

    @field_validator("registration_deadline")
    @classmethod
    def aware_deadline(cls, value):
        if value.tzinfo is None:
            raise ValueError("Registration deadline must include a timezone")
        return value

    @field_validator("vip_code_hashes")
    @classmethod
    def valid_hashes(cls, values):
        import re

        if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in values):
            raise ValueError("VIP codes must be lowercase SHA-256 hex hashes")
        return values


@lru_cache
def get_settings():
    return Settings()
