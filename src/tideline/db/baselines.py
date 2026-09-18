"""Reading and writing DNS baselines.

The DNS check is a pure function, so it cannot read the database. The runner
loads the baseline before the check and stores one after the first run; this
module is the only place that touches the table.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tideline.db.models import DnsBaseline

Records = dict[str, dict[str, list[str]]]


async def get_baseline(session: AsyncSession, site_id: int) -> Records | None:
    """The accepted records for a site, or None when none has been captured yet."""
    row = await session.scalar(
        select(DnsBaseline)
        .where(DnsBaseline.site_id == site_id)
        .order_by(DnsBaseline.accepted_at.desc(), DnsBaseline.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    records: Records = row.records
    return records


async def set_baseline(
    session: AsyncSession, site_id: int, records: dict[str, Any], now: datetime
) -> DnsBaseline:
    """Accept these records as the baseline.

    History is kept: a new row is added rather than the old one edited, so
    "what did DNS look like before the change" stays answerable.
    """
    baseline = DnsBaseline(site_id=site_id, records=records, accepted_at=now)
    session.add(baseline)
    await session.flush()
    return baseline
