"""Settings, read from environment variables (prefix TIDELINE_).

Locally these come from the environment or a .env file. On Lambda the
non-secret ones are set on the function (infra/lambda.tf), and the secrets are
read from SSM Parameter Store when the function starts (tideline.aws_lambda),
so nothing secret is ever in the repo, the image or the function's settings.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TIDELINE_", env_file=".env", extra="ignore")

    # SQLAlchemy URL of the SQLite file. On Lambda it is a copy in /tmp that
    # tideline.db.store downloads from S3 and uploads back (docs/decisions/0005).
    database_url: str = "sqlite+aiosqlite:///tideline.db"

    log_level: str = "INFO"
    user_agent: str = "Tideline/1.0 (+https://obwebdesign.ca)"

    # Where the database file lives between runs on Lambda (tideline.db.store).
    # Empty means "just use database_url as it is" (local development).
    db_bucket: str = ""
    db_key: str = "tideline.db"
    # SSM Parameter Store path holding the secrets and the site list.
    secrets_prefix: str = ""

    # Runs
    max_concurrent_checks: int = 5
    # Seconds between the first uptime failure and its one retry (README section 2).
    uptime_retry_delay_seconds: float = 30.0

    # API and dashboard. Both credentials are required for the API to start:
    # in production they are read from SSM Parameter Store.
    api_token: str = ""
    dashboard_user: str = "owen"
    dashboard_password: str = ""
    # `tideline api` (local development) listens here.
    api_host: str = "127.0.0.1"
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
