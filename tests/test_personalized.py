from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ous_monitor.personalized import (
    dispatch_personalized_alerts,
    event_key,
    select_personalized_changes,
)
from ous_monitor.storage import connect, set_alert_preferences


def _change(category="new_promo", sku="sku-1"):
    return {
        "source": "ous",
        "sku": sku,
        "name": "Tenis Teste",
        "url": "https://example.test/item",
        "price": 100.0,
        "list_price": 200.0,
        "prev_price": 150.0,
        "prev_list_price": 200.0,
        "observed_at": "2026-09-02T12:00:00+00:00",
        "sizes": None,
        "stock_qty": None,
        "category": category,
    }


class PersonalizedSelectionTests(unittest.TestCase):
    def test_favorites_receive_all_events_and_saved_filters_only_new_promos(self):
        changes = {
            "new_promo": [_change(sku="filter"), _change(sku="favorite")],
            "ended": [_change("ended", "filter"), _change("ended", "favorite")],
            "weaker": [],
            "price_up": [],
        }

        selected, keys = select_personalized_changes(
            changes,
            favorite_keys={("ous", "favorite")},
            saved_filter_keys={("ous", "filter")},
        )

        self.assertEqual(
            [row["sku"] for row in selected["new_promo"]],
            ["filter", "favorite"],
        )
        self.assertEqual([row["sku"] for row in selected["ended"]], ["favorite"])
        self.assertEqual(len(keys), 3)

    def test_event_key_is_stable_and_delivery_filter_is_applied(self):
        row = _change()
        key = event_key("new_promo", row)
        selected, keys = select_personalized_changes(
            {"new_promo": [row], "ended": [], "weaker": [], "price_up": []},
            favorite_keys={("ous", "sku-1")},
            saved_filter_keys=set(),
            already_delivered=lambda candidate: candidate == key,
        )

        self.assertTrue(key.startswith("v1:"))
        self.assertEqual(keys, [])
        self.assertEqual(selected["new_promo"], [])


class PersonalizedDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "prices.db"

    def tearDown(self):
        self.tmp.cleanup()

    @patch("ous_monitor.personalized._saved_filter_product_keys")
    @patch("ous_monitor.personalized.list_favorites")
    @patch("ous_monitor.personalized.list_saved_filters")
    def test_dispatch_respects_hour_and_deduplicates(
        self, list_saved, list_favorites, filter_keys
    ):
        with connect(self.db) as conn:
            set_alert_preferences(conn, 123, enabled=True, hour_utc=12)
        list_saved.return_value = [{"id": 1}]
        list_favorites.return_value = []
        filter_keys.return_value = {("ous", "sku-1")}
        sender = Mock(return_value=1)
        changes = {
            "new_promo": [_change()], "ended": [], "weaker": [], "price_up": [],
        }

        early = dispatch_personalized_alerts(
            self.db, changes, bot_token="token", hour_utc=11, sender=sender,
        )
        first = dispatch_personalized_alerts(
            self.db, changes, bot_token="token", hour_utc=12, sender=sender,
        )
        repeated = dispatch_personalized_alerts(
            self.db, changes, bot_token="token", hour_utc=12, sender=sender,
        )

        self.assertEqual(early.events, 0)
        self.assertEqual((first.chats, first.events), (1, 1))
        self.assertEqual(repeated.events, 0)
        sender.assert_called_once()


if __name__ == "__main__":
    unittest.main()
