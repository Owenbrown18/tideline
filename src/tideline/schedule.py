"""When Tideline runs: on the 1st and 15th of each month, 07:00 Pacific.

The schedule itself lives in AWS (EventBridge Scheduler, infra/lambda.tf). These
constants are the same schedule, for the places that talk about it: the
dashboard's "Next check", and the monthly report on the 1st.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

RUN_DAYS = (1, 15)
RUN_TIME = time(7, 0)


def next_run(now: datetime, zone: str = "America/Vancouver") -> datetime:
    """The next scheduled run after `now`, as an aware datetime in `zone`."""
    local = now.astimezone(ZoneInfo(zone))
    day = local.date()
    for _ in range(40):
        if day.day in RUN_DAYS:
            candidate = datetime.combine(day, RUN_TIME, tzinfo=ZoneInfo(zone))
            if candidate > local:
                return candidate
        day += timedelta(days=1)
    raise AssertionError("no run day within 40 days")  # unreachable: runs are monthly


def next_report(now: datetime, zone: str = "America/Vancouver") -> datetime:
    """When the next monthly reports go out: the next run on the 1st."""
    run = next_run(now, zone)
    while run.day != 1:
        run = next_run(run, zone)
    return run


def is_report_day(now: datetime, zone: str = "America/Vancouver") -> bool:
    """Monthly reports go out with the run on the 1st."""
    return now.astimezone(ZoneInfo(zone)).day == 1
