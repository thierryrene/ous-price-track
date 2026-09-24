from __future__ import annotations

import unittest

from ous_monitor.bot.callbacks import MAX_CALLBACK_DATA_BYTES, decode_to_legacy
from ous_monitor.bot.views import (
    build_alert_preferences_view,
    build_favorite_button,
    build_favorites_view,
    build_save_filter_button,
    build_saved_filters_view,
)


def callback_values(markup):
    return [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]


class BotViewsTests(unittest.TestCase):
    def assert_valid_callbacks(self, markup):
        callbacks = callback_values(markup)
        self.assertTrue(callbacks)
        for callback in callbacks:
            self.assertLessEqual(len(callback.encode("utf-8")), MAX_CALLBACK_DATA_BYTES)

    def test_saved_filters_escape_content_and_offer_actions(self):
        text, markup = build_saved_filters_view(
            [
                {
                    "id": 7,
                    "name": "Tênis <barato>",
                    "source": "Loja & Cia",
                    "criteria": {
                        "category": "tênis & skate",
                        "max_price": "200",
                        "min_discount": "30",
                    },
                }
            ]
        )

        self.assertIn("Tênis &lt;barato&gt;", text)
        self.assertIn("Loja &amp; Cia", text)
        self.assertIn("tênis &amp; skate", text)
        legacy = [decode_to_legacy(value) for value in callback_values(markup)]
        self.assertIn("saved:load:7", legacy)
        self.assertIn("saved:delete:7", legacy)
        self.assertIn("run:back", legacy)
        self.assert_valid_callbacks(markup)

    def test_saved_filters_empty_state_is_actionable(self):
        text, markup = build_saved_filters_view([])

        self.assertIn("ainda não salvou", text)
        self.assertIn("Salvar filtro", text)
        self.assertEqual(decode_to_legacy(callback_values(markup)[0]), "run:back")

    def test_favorites_render_prices_links_removal_and_pagination(self):
        rows = [
            {
                "ref": index,
                "name": f"Produto <{index}>",
                "url": f"https://example.test/{index}",
                "price": 99.9 + index,
                "list_price": 200,
                "source": "Loja & Cia",
            }
            for index in range(1, 8)
        ]

        text, markup = build_favorites_view(rows, page=1, page_size=5)

        self.assertIn("página 2/2", text)
        self.assertIn("Produto &lt;6&gt;", text)
        self.assertNotIn("Produto &lt;1&gt;", text)
        self.assertIn("Loja &amp; Cia", text)
        self.assertIn("R$ 105,90", text)
        self.assertIn("<s>R$ 200,00</s>", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["url"], "https://example.test/6")
        legacy = [decode_to_legacy(value) for value in callback_values(markup)]
        self.assertIn("favorite:delete:6", legacy)
        self.assertIn("favorites:page:0", legacy)
        self.assertNotIn("favorites:page:2", legacy)
        self.assert_valid_callbacks(markup)

    def test_favorites_empty_state_and_invalid_page_size(self):
        text, markup = build_favorites_view([])
        self.assertIn("lista de favoritos está vazia", text)
        self.assertEqual(decode_to_legacy(callback_values(markup)[0]), "run:back")

        with self.assertRaises(ValueError):
            build_favorites_view([], page_size=0)

    def test_alert_preferences_render_toggle_hours_and_custom_selection(self):
        text, markup = build_alert_preferences_view(True, 9)

        self.assertIn("Ativados", text)
        self.assertIn("09:00 UTC", text)
        self.assertIn("06:00 BRT", text)
        button_texts = [button["text"] for row in markup["inline_keyboard"] for button in row]
        self.assertIn("✅ 09:00", button_texts)
        legacy = [decode_to_legacy(value) for value in callback_values(markup)]
        self.assertIn("alerts:toggle", legacy)
        self.assertIn("alerts:hour:9", legacy)
        self.assertIn("alerts:hour:21", legacy)
        self.assert_valid_callbacks(markup)

        paused_text, paused_markup = build_alert_preferences_view(False, 18)
        self.assertIn("Pausados", paused_text)
        self.assertEqual(paused_markup["inline_keyboard"][0][0]["text"], "▶ Ativar alertas")

    def test_alert_preferences_reject_invalid_hour(self):
        with self.assertRaises(ValueError):
            build_alert_preferences_view(True, 24)

    def test_reusable_card_buttons_use_versioned_callbacks(self):
        save = build_save_filter_button("ous")
        favorite = build_favorite_button(42)
        selected = build_favorite_button(42, selected=True)

        self.assertEqual(decode_to_legacy(save["callback_data"]), "saved:create:ous")
        self.assertEqual(decode_to_legacy(favorite["callback_data"]), "favorite:toggle:42")
        self.assertEqual(selected["text"], "★ Favorito")
        for button in (save, favorite, selected):
            self.assertLessEqual(
                len(button["callback_data"].encode("utf-8")),
                MAX_CALLBACK_DATA_BYTES,
            )


if __name__ == "__main__":
    unittest.main()
