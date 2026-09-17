"""Alerting. `build_notifier` picks the channel from settings."""

from sitewatch.config import Settings
from sitewatch.notify.base import AlertMessage, LogNotifier, Notifier

__all__ = ["AlertMessage", "LogNotifier", "Notifier", "build_notifier"]


def build_notifier(settings: Settings) -> Notifier:
    if settings.notify_channel == "ses":
        # Imported here so local runs and tests need no AWS client at all.
        from sitewatch.notify.ses import SesNotifier

        return SesNotifier(settings.aws_region, settings.alert_sender, settings.alert_email)
    if settings.notify_channel == "log":
        return LogNotifier()
    raise ValueError(f"unknown SITEWATCH_NOTIFY_CHANNEL: {settings.notify_channel!r}")
