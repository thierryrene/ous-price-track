from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks
from ous_monitor.bot.callbacks import decode_to_legacy
from ous_monitor.server import (
    CATALOG_PAGE_SIZE,
    _active_updates,
    _edit_or_send_text,
    _filtered_keyboard,
    _needs_catalog_refresh,
    _release_update,
    _reserve_update,
    run_filtered_task,
    run_personalized_alert_dispatch,
    telegram_webhook,
)
from ous_monitor.storage import (
    connect, get_alert_preferences, list_saved_filters, save_bot_session,
)


class FastCatalogQueryTests(unittest.TestCase):
    @patch("ous_monitor.server.MonitorService")
    @patch("ous_monitor.server._render_text_sync")
    @patch("ous_monitor.server.CatalogService.source_freshness")
    @patch("ous_monitor.server.CatalogService.latest_discounted")
    def test_filtered_query_never_starts_scraper(
        self, latest_discounted, source_freshness, render_text, monitor_service
    ):
        latest_discounted.return_value = [{
            "name": "Tênis Teste", "price": 100.0, "list_price": 200.0,
            "url": "https://example.test/item",
        }]
        source_freshness.return_value = SimpleNamespace(
            source="ous", last_success_at="2026-08-24T12:00:00+00:00",
            checked_at="2026-08-24T12:00:00+00:00", age_seconds=60,
            successful_run_id="run-1", has_snapshot=True, status="success",
        )

        run_filtered_task("ous", {}, "token", "1", message_id=77)

        monitor_service.assert_not_called()
        latest_discounted.assert_called_once()
        render_text.assert_called_once()
        self.assertEqual(render_text.call_args.args[2], 77)
        rendered = render_text.call_args.args[3]
        self.assertNotIn("🆕", rendered)
        self.assertIn("Preços verificados", rendered)

    def test_results_offer_explicit_refresh(self):
        callbacks = {
            decode_to_legacy(button["callback_data"])
            for row in _filtered_keyboard("ous")["inline_keyboard"]
            for button in row
        }
        self.assertIn("filter:ous:refresh", callbacks)
        self.assertIn("run:ous", callbacks)

    def test_results_keyboard_paginates_without_scraping(self):
        callbacks = {
            decode_to_legacy(button["callback_data"])
            for row in _filtered_keyboard(
                "ous", offset=CATALOG_PAGE_SIZE, has_more=True,
            )["inline_keyboard"]
            for button in row
        }
        self.assertIn("offers:ous:0", callbacks)
        self.assertIn(f"offers:ous:{CATALOG_PAGE_SIZE * 2}", callbacks)

    def test_results_keyboard_exposes_numbered_favorite_actions(self):
        keyboard = _filtered_keyboard(
            "ous", favorites=[(1, "abc123", False), (2, "def456", True)],
        )
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        callbacks = {decode_to_legacy(button["callback_data"]) for button in buttons}

        self.assertIn("favorite:toggle:abc123", callbacks)
        self.assertIn("favorite:toggle:def456", callbacks)
        self.assertTrue(any(button["text"] == "☆ Favoritar · item 1" for button in buttons))


class UpdateReservationTests(unittest.TestCase):
    def tearDown(self):
        _active_updates.clear()

    def test_prevents_duplicate_and_conflicting_updates(self):
        self.assertTrue(_reserve_update("ous"))
        self.assertFalse(_reserve_update("ous"))
        self.assertFalse(_reserve_update("baw"))
        self.assertFalse(_reserve_update("*"))
        _release_update("ous")
        self.assertTrue(_reserve_update("*"))
        self.assertFalse(_reserve_update("baw"))

    @patch.dict("os.environ", {"CATALOG_FRESH_HOURS": "2"})
    def test_revalidates_only_stale_or_failed_snapshot(self):
        fresh = SimpleNamespace(
            has_snapshot=True, status="success", age_seconds=60,
        )
        stale = SimpleNamespace(
            has_snapshot=True, status="success", age_seconds=7201,
        )
        failed = SimpleNamespace(
            has_snapshot=True, status="failed", age_seconds=60,
        )

        self.assertFalse(_needs_catalog_refresh(fresh))
        self.assertTrue(_needs_catalog_refresh(stale))
        self.assertTrue(_needs_catalog_refresh(failed))
        self.assertTrue(_needs_catalog_refresh(None))


class PersonalizedScheduleTests(unittest.TestCase):
    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "token"}, clear=True)
    @patch("ous_monitor.server.find_changes", return_value={})
    @patch("ous_monitor.server.dispatch_personalized_alerts")
    def test_dispatch_includes_current_and_previous_hour(self, dispatch, find_changes):
        dispatch.return_value = SimpleNamespace(chats=0, events=0)
        with tempfile.TemporaryDirectory() as tmp, patch(
            "ous_monitor.server.DEFAULT_DB", Path(tmp) / "prices.db"
        ):
            run_personalized_alert_dispatch(
                datetime(2026, 9, 2, 13, 5, tzinfo=timezone.utc)
            )

        self.assertEqual(
            {call.kwargs["hour_utc"] for call in dispatch.call_args_list},
            {12, 13},
        )
        find_changes.assert_called_once()


class _TelegramResponse:
    def __init__(self, status_code=200, description=""):
        self.status_code = status_code
        self.text = description
        self._description = description

    def json(self):
        return {"ok": self.status_code == 200, "description": self._description}


class TelegramEditTests(unittest.IsolatedAsyncioTestCase):
    @patch("ous_monitor.server._telegram_post", new_callable=AsyncMock)
    async def test_identical_edit_does_not_create_duplicate_message(self, post):
        post.return_value = _TelegramResponse(400, "Bad Request: message is not modified")

        await _edit_or_send_text("token", 1, 77, "mesmo texto")

        self.assertEqual(post.await_count, 1)
        self.assertEqual(post.await_args.args[1], "editMessageText")

    @patch("ous_monitor.server._telegram_post", new_callable=AsyncMock)
    async def test_uneditable_message_falls_back_to_one_new_message(self, post):
        post.side_effect = [
            _TelegramResponse(400, "Bad Request: message to edit not found"),
            _TelegramResponse(200),
        ]

        await _edit_or_send_text("token", 1, 77, "nova tela")

        self.assertEqual(
            [call.args[1] for call in post.await_args_list],
            ["editMessageText", "sendMessage"],
        )


class _WebhookRequest:
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class TelegramNavigationTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"},
        clear=True,
    )
    @patch("ous_monitor.server._telegram_post", new_callable=AsyncMock)
    async def test_store_menu_edits_callback_message(self, post):
        post.return_value = _TelegramResponse(200)
        request = _WebhookRequest({
            "callback_query": {
                "id": "callback-1",
                "data": "1.stores.menu",
                "message": {"message_id": 77, "chat": {"id": 123}},
            }
        })

        response = await telegram_webhook(request, BackgroundTasks())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["status"], "store_menu_sent")
        methods = [call.args[1] for call in post.await_args_list]
        self.assertEqual(methods, ["answerCallbackQuery", "editMessageText"])
        self.assertEqual(post.await_args_list[-1].args[2]["message_id"], 77)

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"},
        clear=True,
    )
    @patch("ous_monitor.server._telegram_post", new_callable=AsyncMock)
    async def test_notification_menu_intentionally_sends_new_message(self, post):
        post.return_value = _TelegramResponse(200)
        request = _WebhookRequest({
            "callback_query": {
                "id": "callback-2",
                "data": "1.home.new",
                "message": {"message_id": 88, "chat": {"id": 123}},
            }
        })

        await telegram_webhook(request, BackgroundTasks())

        methods = [call.args[1] for call in post.await_args_list]
        self.assertEqual(methods, ["answerCallbackQuery", "sendMessage"])

    @patch.dict(
        "os.environ",
        {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_CHAT_IDS": "123"},
        clear=True,
    )
    @patch("ous_monitor.server._telegram_post", new_callable=AsyncMock)
    async def test_saved_filter_and_alert_preference_callbacks_persist(self, post):
        post.return_value = _TelegramResponse(200)

        async def run_direct(func, *args, **kwargs):
            return func(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            with connect(db) as conn:
                save_bot_session(conn, 123, {
                    "source": "ous", "category": "tenis",
                    "max_price": "200", "min_discount": "30",
                })

            with patch("ous_monitor.server.DEFAULT_DB", db), patch(
                "ous_monitor.server._run_sync", side_effect=run_direct,
            ):
                create = await telegram_webhook(
                    _WebhookRequest({
                        "callback_query": {
                            "id": "save-1", "data": "1.saved.create.ous",
                            "message": {"message_id": 77, "chat": {"id": 123}},
                        }
                    }),
                    BackgroundTasks(),
                )
                toggle = await telegram_webhook(
                    _WebhookRequest({
                        "callback_query": {
                            "id": "alerts-1", "data": "1.alerts.toggle",
                            "message": {"message_id": 77, "chat": {"id": 123}},
                        }
                    }),
                    BackgroundTasks(),
                )

            self.assertEqual(json.loads(create.body)["status"], "saved_filter_created")
            self.assertEqual(json.loads(toggle.body)["status"], "alert_preferences_updated")
            with connect(db) as conn:
                saved = list_saved_filters(conn, 123)
                preference = get_alert_preferences(conn, 123)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["category"], "tenis")
            self.assertTrue(preference["enabled"])


if __name__ == "__main__":
    unittest.main()
