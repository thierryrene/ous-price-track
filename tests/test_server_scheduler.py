from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ous_monitor.server import _latest_due_monitor_slot, _monitor_schedule
from ous_monitor.storage import connect, get_scheduler_slot, set_scheduler_slot


class ServerSchedulerTests(unittest.TestCase):
    def test_default_schedule(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MONITOR_SCHEDULE_UTC", None)
            self.assertEqual(_monitor_schedule(), [(12, "alert"), (21, "digest")])

    def test_custom_schedule_is_sorted_and_validated(self) -> None:
        with patch.dict(
            os.environ,
            {"MONITOR_SCHEDULE_UTC": "21:digest,garbage,7:alert,25:alert"},
        ):
            self.assertEqual(_monitor_schedule(), [(7, "alert"), (21, "digest")])

    def test_latest_due_slot_uses_previous_day_before_first_hour(self) -> None:
        with patch.dict(
            os.environ,
            {"MONITOR_SCHEDULE_UTC": "12:alert,21:digest"},
        ):
            slot, mode = _latest_due_monitor_slot(
                datetime(2026, 9, 24, 8, 30, tzinfo=timezone.utc),
            )
        self.assertEqual(slot, "2026-09-23T21:00Z")
        self.assertEqual(mode, "digest")

    def test_latest_due_slot_uses_current_day(self) -> None:
        with patch.dict(
            os.environ,
            {"MONITOR_SCHEDULE_UTC": "12:alert,21:digest"},
        ):
            slot, mode = _latest_due_monitor_slot(
                datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(slot, "2026-09-24T12:00Z")
        self.assertEqual(mode, "alert")

    def test_scheduler_slot_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            with connect(db) as conn:
                self.assertIsNone(get_scheduler_slot(conn, "catalog_monitor"))
                set_scheduler_slot(conn, "catalog_monitor", "2026-09-24T12:00Z")
            with connect(db) as conn:
                self.assertEqual(
                    get_scheduler_slot(conn, "catalog_monitor"),
                    "2026-09-24T12:00Z",
                )

    def test_later_manual_slot_covers_earlier_scheduled_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            with connect(db) as conn:
                set_scheduler_slot(conn, "catalog_monitor", "2026-09-24T14:24Z")
                self.assertGreaterEqual(
                    get_scheduler_slot(conn, "catalog_monitor"),
                    "2026-09-24T12:00Z",
                )


if __name__ == "__main__":
    unittest.main()
