"""Background re-indexing.

Polls the allow-listed folders for changed modification times and sizes, and
re-indexes only when that cheap snapshot differs. Polling rather than OS
file-system events keeps this dependency-free and behaves the same on Windows,
macOS, and Linux; the snapshot never reads file contents, so an idle check over
a notes folder costs a directory walk and nothing else.

Set `BILLIE_WATCH_INTERVAL_SECONDS=0` to turn it off.
"""

from __future__ import annotations

import asyncio
import logging

from billie.memory.indexer import root_signature
from billie.permissions import CALENDAR_READ, FILE_READ

logger = logging.getLogger("billie.watcher")


class FileWatcher:
    def __init__(self, assistant, interval_seconds: float) -> None:
        self.assistant = assistant
        self.interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._signature: dict | None = None

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def snapshot(self) -> dict:
        settings = self.assistant.settings
        return root_signature(
            settings.index_roots(), max_bytes=settings.max_indexed_file_bytes
        )

    def check_once(self) -> dict | None:
        """Re-index if anything changed. Returns the report, or None."""
        if not self.assistant.permissions.is_granted(FILE_READ):
            return None
        current = self.snapshot()
        if self._signature is not None and current == self._signature:
            return None
        changed = self._signature is not None
        self._signature = current
        if not changed:
            # First run after start: the startup index already covered this.
            return None
        report = self.assistant.reindex_files()
        self.assistant.audit.record("watcher.reindex", **report.to_dict())
        logger.info("Watcher re-indexed: %s", report.to_dict())
        return report

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    await asyncio.to_thread(self.check_once)
                except Exception:  # noqa: BLE001 - a bad file must not kill the loop
                    logger.exception("Watcher pass failed")
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._signature = self.snapshot()
        self._task = asyncio.create_task(self._loop())
        logger.info("Watching indexed folders every %ss", self.interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


class CalendarSyncTask:
    """Pulls calendar events into memory on an interval.

    Read-only against Google: it never creates, moves, or deletes anything.
    Off when no Google account is configured, when `calendar.read` is revoked,
    or when the interval is 0.
    """

    def __init__(self, assistant, interval_seconds: float) -> None:
        self.assistant = assistant
        self.interval = interval_seconds
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.interval > 0 and self.assistant.calendar is not None

    async def check_once(self):
        """Sync if it is allowed to. Returns the report, or None."""
        if self.assistant.calendar is None:
            return None
        if not self.assistant.permissions.is_granted(CALENDAR_READ):
            return None
        if not self.assistant.calendar.oauth.connected():
            return None
        report = await self.assistant.sync_calendar()
        logger.info("Calendar synced: %s", report.to_dict())
        return report

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                try:
                    await self.check_once()
                except Exception:  # noqa: BLE001 - a bad sync must not kill the loop
                    logger.exception("Calendar sync failed")
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("Syncing the calendar every %ss", self.interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
