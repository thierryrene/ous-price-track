from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ous_monitor.notifier import (
    CATEGORY_KEYBOARD,
    MENU_KEYBOARD,
    SOURCE_LABEL_SHORT,
    STORE_KEYBOARD,
    build_filter_keyboard,
)
from ous_monitor.server import (
    _is_allowed_chat,
    get_store_status,
    run_automatic_maintenance,
)
from ous_monitor.sources import SOURCES


def _buttons(keyboard: dict) -> list[dict]:
    return [
        button
        for row in keyboard["inline_keyboard"]
        for button in row
    ]


class TelegramMenuTests(unittest.TestCase):
    def test_store_menu_contains_every_registered_source_once(self):
        source_callbacks = [
            button["callback_data"]
            for button in _buttons(STORE_KEYBOARD)
            if button["callback_data"].startswith("run:")
            and button["callback_data"] not in {"run:all", "run:back"}
        ]
        expected = [f"run:{source}" for source in SOURCES]

        self.assertCountEqual(source_callbacks, expected)
        self.assertEqual(len(source_callbacks), len(set(source_callbacks)))
        self.assertEqual(set(SOURCE_LABEL_SHORT), set(SOURCES))

    def test_all_callback_data_fit_telegram_limit(self):
        keyboards = [
            MENU_KEYBOARD,
            STORE_KEYBOARD,
            CATEGORY_KEYBOARD,
            build_filter_keyboard(
                "netshoes_adidas_originals",
                {"category": "all", "max_price": "all", "min_discount": "all"},
            ),
        ]

        for keyboard in keyboards:
            for button in _buttons(keyboard):
                callback = button["callback_data"]
                self.assertLessEqual(len(callback.encode("utf-8")), 64, callback)

    def test_daily_menu_exposes_special_clothing_categories(self):
        callbacks = {button["callback_data"] for button in _buttons(CATEGORY_KEYBOARD)}

        self.assertIn("run:daily_promos:camisas_time", callbacks)
        self.assertIn("run:daily_promos:agasalhos", callbacks)

    @patch("ous_monitor.server.CatalogService.store_status")
    def test_status_lists_sources_without_data(self, store_status):
        store_status.return_value = [
            {"source": "ous", "products": 12, "newest": "2026-07-28T12:00:00"}
        ]

        text = get_store_status()

        for label in SOURCE_LABEL_SHORT.values():
            self.assertIn(label, text)
        self.assertIn("sem dados coletados", text)


class TelegramAuthorizationTests(unittest.TestCase):
    def test_explicit_allowlist_has_priority(self):
        with patch.dict(
            os.environ,
            {"TELEGRAM_ALLOWED_CHAT_IDS": "10,20", "TELEGRAM_CHAT_ID": "30"},
            clear=True,
        ):
            self.assertTrue(_is_allowed_chat(10))
            self.assertFalse(_is_allowed_chat(30))

    def test_main_chat_is_fallback_allowlist(self):
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "30"}, clear=True):
            self.assertTrue(_is_allowed_chat("30"))
            self.assertFalse(_is_allowed_chat("31"))

    def test_missing_chat_configuration_denies_access(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_is_allowed_chat(10))

    @patch("ous_monitor.server.CatalogService.maintain")
    def test_recent_backup_prevents_duplicate_automatic_maintenance(self, maintain):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            backup_dir = db.parent / "backups"
            backup_dir.mkdir()
            (backup_dir / "prices-20260728T000000.000000Z.db").touch()
            with patch("ous_monitor.server.DEFAULT_DB", db):
                with patch.dict(
                    os.environ,
                    {"MAINTENANCE_INTERVAL_HOURS": "24"},
                    clear=True,
                ):
                    self.assertFalse(run_automatic_maintenance())
        maintain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
