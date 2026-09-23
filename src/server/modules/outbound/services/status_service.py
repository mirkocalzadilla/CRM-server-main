"""Applies Meta webhook `statuses` (sent/delivered/read/failed) to outbound rows.

Statuses can arrive out of order and duplicated; precedence is by rank so a late
`sent` never downgrades `delivered`, and `failed` always wins (it is terminal).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from server.modules.outbound.domain.models import STATUS_FAILED, status_rank
from server.modules.outbound.repositories.outbound_repository import OutboundMessageRepository
from server.shared.logger import get_logger

logger = get_logger(__name__)

_KNOWN = {"sent", "delivered", "read", "failed"}


def _status_time(raw: object) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(raw)), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _first_error(status: Mapping[str, object]) -> tuple[int | None, str]:
    errors = status.get("errors")
    first = errors[0] if isinstance(errors, list) and errors else None
    if not isinstance(first, Mapping):
        return None, ""
    code = first.get("code")
    detail = str(first.get("title") or first.get("message") or "")
    return (int(str(code)) if code is not None else None), detail


class OutboundStatusService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = OutboundMessageRepository(session)

    async def apply(self, statuses: Sequence[Mapping[str, object]]) -> int:
        """Returns how many outbound rows were updated."""
        updated = 0
        for status in statuses:
            new_status = str(status.get("status", ""))
            wamid = status.get("id")
            if new_status not in _KNOWN or not isinstance(wamid, str):
                continue
            row = await self._repo.get_by_wamid(wamid)
            if row is None:
                continue  # free-form agent/human messages are not tracked here
            if status_rank(new_status) < status_rank(row.status):
                continue
            row.status = new_status
            row.status_at = _status_time(status.get("timestamp"))
            if new_status == STATUS_FAILED:
                row.error_code, row.error_detail = _first_error(status)
                logger.warning(
                    "outbound.failed",
                    wamid=wamid,
                    template=row.template_name,
                    error_code=row.error_code,
                )
            updated += 1
        return updated
