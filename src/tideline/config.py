"""Settings, read from environment variables (prefix TIDELINE_).

Locally these come from compose.yaml or a .env file. In production the deploy
script writes them from SSM Parameter Store, so nothing secret is ever in the
repo or baked into the image.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TIDELINE_", env_file=".env", extra="ignore")

    # SQLAlchemy URL. psycopg 3 serves both the async app and sync Alembic.
    database_url: str = "postgresql+psycopg://sitewatch:sitewatch@localhost:5432/sitewatch"

    log_level: str = "INFO"
    user_agent: str = "Tideline/1.0 (+https://obwebdesign.ca)"

    # Worker behaviour
    max_concurrent_checks: int = 5
    # Seconds between the first uptime failure and its one retry (README section 2).
    uptime_retry_delay_seconds: float = 30.0
    # How often the worker re-reads the checks table to pick up added/removed checks.
    schedule_refresh_seconds: int = 60
    # How often the worker logs its heartbeat.
    heartbeat_seconds: int = 60
    # Hours between reminder alerts for an incident that stays open.
    reminder_hours: int = 24

    # API and dashboard. Both credentials are required for the API to start:
    # in production the deploy script reads them from SSM Parameter Store.
    api_token: str = ""
    dashboard_user: str = "owen"
    dashboard_password: str = ""
    # Binds inside the container only: Caddy is the only public listener.
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Alerts. "log" writes them as log lines (local development), "ses" emails
    # them to alert_email. Tideline has no client addresses: alerts go to Owen.
    notify_channel: str = "log"
    alert_email: str = "owenjosephbrown@gmail.com"
    alert_sender: str = "Tideline <tideline@obwebdesign.ca>"
    # The dashboard's public address, for the "Open in Tideline" link in alerts.
    # Empty leaves the link out (local development).
    public_url: str = ""
    aws_region: str = "ca-central-1"

    # Times people read (dashboard, emails, reports) are shown in this zone.
    # Everything stored and every API field stays in UTC.
    display_timezone: str = "America/Vancouver"

    # Where RDAP lookups go. rdap.org redirects to the right registry (CIRA for .ca).
    rdap_base_url: str = "https://rdap.org/domain/"


@lru_cache
def get_settings() -> Settings:
    return Settings()
