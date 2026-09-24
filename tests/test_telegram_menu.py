from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ous_monitor.bot.callbacks import decode_to_legacy
from ous_monitor.notifier import (
    CATEGORY_KEYBOARD,
    MENU_KEYBOARD,
    NOTIFICATION_KEYBOARD,
    SOURCE_LABEL_SHORT,
    STORE_KEYBOARD,
    UPDATE_KEYBOARD,
    build_filter_message,
    build_filter_keyboard,
    build_freshness_message,
    build_progress_message,
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


def _callback(button: dict) -> str:
    return decode_to_legacy(button["callback_data"])


class TelegramMenuTests(unittest.TestCase):
    def test_store_menu_contains_every_registered_source_once(self):
        source_callbacks = [
            _callback(button)
            for button in _buttons(STORE_KEYBOARD)
            if _callback(button).startswith("run:")
            and _callback(button) not in {"run:all", "run:back"}
        ]
        expected = [f"run:{source}" for source in SOURCES]

        self.assertCountEqual(source_callbacks, expected)
        self.assertEqual(len(source_callbacks), len(set(source_callbacks)))
        self.assertEqual(set(SOURCE_LABEL_SHORT), set(SOURCES))

    def test_all_callback_data_fit_telegram_limit(self):
        keyboards = [
            MENU_KEYBOARD,
            NOTIFICATION_KEYBOARD,
            STORE_KEYBOARD,
            UPDATE_KEYBOARD,
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
                self.assertTrue(callback.startswith("1."), callback)

    def test_notification_opens_new_menu_without_overwriting_alert(self):
        callbacks = {
            _callback(button) for button in _buttons(NOTIFICATION_KEYBOARD)
        }

        self.assertEqual(callbacks, {"home:new"})

    def test_main_menu_separates_catalog_queries_from_updates(self):
        callbacks = {_callback(button) for button in _buttons(MENU_KEYBOARD)}

        self.assertIn("stores:menu", callbacks)
        self.assertIn("update:menu", callbacks)
        self.assertIn("saved:list", callbacks)
        self.assertIn("favorites:page:0", callbacks)
        self.assertIn("alerts:prefs", callbacks)
        self.assertNotIn("run:all", callbacks)
        self.assertNotIn("run:snapshot", callbacks)

    def test_filter_menu_offers_cached_query_and_explicit_refresh(self):
        keyboard = build_filter_keyboard(
            "ous", {"category": "all", "max_price": "all", "min_discount": "30"}
        )
        buttons = _buttons(keyboard)
        callbacks = {_callback(button) for button in buttons}
        labels = {button["text"] for button in buttons}

        self.assertIn("filter:ous:run", callbacks)
        self.assertIn("filter:ous:refresh", callbacks)
        self.assertIn("saved:create:ous", callbacks)
        self.assertTrue(any("30% ou mais" in label for label in labels))
        self.assertIn(
            "Desconto: <b>30% ou mais</b>",
            build_filter_message("ous", {"min_discount": "30"}),
        )

    def test_filter_menu_can_show_live_option_counts(self):
        keyboard = build_filter_keyboard(
            "ous",
            {"category": "all", "max_price": "all", "min_discount": "all"},
            counts={
                "category": {"tenis": 12, "vestuario": 8, "acessorios": 3,
                             "camisas_time": 2, "agasalhos": 4, "all": 29},
                "max_price": {"100": 5, "200": 14, "500": 27, "all": 29},
                "min_discount": {"50": 6, "30": 19, "all": 29},
            },
        )
        labels = {button["text"] for button in _buttons(keyboard)}

        self.assertTrue(any("Tênis · 12" in label for label in labels))
        self.assertTrue(any("Até R$200 · 14" in label for label in labels))
        self.assertTrue(any("50% ou mais · 6" in label for label in labels))

    def test_freshness_and_progress_are_user_facing(self):
        freshness = build_freshness_message(
            "ous", "2026-08-24T10:00:00+00:00",
            now="2026-08-24T10:37:00+00:00",
        )
        progress = build_progress_message(
            4, 9, current="netshoes_adidas", page=113, total_pages=197,
            elapsed_seconds=300, eta_seconds=240,
        )

        self.assertIn("🟢", freshness)
        self.assertIn("há 37 min", freshness)
        self.assertIn("4 de 9", progress)
        self.assertIn("página 113/197", progress)
        self.assertIn("estimativa: ~4m00s", progress)

    def test_daily_menu_exposes_special_clothing_categories(self):
        callbacks = {_callback(button) for button in _buttons(CATEGORY_KEYBOARD)}

        self.assertIn("run:daily_promos:camisas_time", callbacks)
        self.assertIn("run:daily_promos:agasalhos", callbacks)

    @patch("ous_monitor.server.CatalogService.freshness")
    @patch("ous_monitor.server.CatalogService.store_status")
    def test_status_lists_sources_without_data(self, store_status, freshness):
        store_status.return_value = [
            {"source": "ous", "products": 12, "newest": "2026-07-28T12:00:00"}
        ]
        freshness.return_value = []

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
