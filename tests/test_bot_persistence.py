from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ous_monitor.models import Product
from ous_monitor.storage import (
    connect,
    delete_favorite,
    delete_saved_filter,
    get_alert_preferences,
    get_or_create_product_ref,
    list_enabled_alert_preferences,
    list_favorites,
    list_saved_filters,
    load_saved_filter,
    record_personalized_alert_delivery,
    record_run,
    resolve_product_ref,
    save_filter,
    set_alert_preferences,
    toggle_favorite,
    was_personalized_alert_delivered,
)


def product(source="ous", sku="sku-1", price=90.0, list_price=120.0):
    return Product(
        source=source,
        sku=sku,
        name=f"Tênis {sku}",
        url=f"https://example.test/{sku}",
        image=f"https://example.test/{sku}.jpg",
        list_price=list_price,
        price=price,
        available=True,
        brand="Teste",
        sizes=["40", "41"],
        stock_qty=3,
    )


class BotPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "prices.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_saved_filters_persist_and_are_isolated_by_chat(self):
        with connect(self.db) as conn:
            saved = save_filter(
                conn,
                100,
                "Tênis baratos",
                source="ous",
                category="tenis",
                max_price="200",
                min_discount="30",
            )
            other = save_filter(
                conn,
                200,
                "Tênis baratos",
                source="adidas",
                category="tenis",
                max_price=300,
            )

        with connect(self.db) as conn:
            restored = load_saved_filter(conn, 100, saved["id"])
            self.assertEqual(restored["name"], "Tênis baratos")
            self.assertEqual(restored["source"], "ous")
            self.assertEqual(restored["max_price"], 200.0)
            self.assertEqual(restored["min_discount"], 30.0)
            self.assertIsNone(load_saved_filter(conn, 200, saved["id"]))
            self.assertEqual([row["id"] for row in list_saved_filters(conn, 200)], [other["id"]])
            self.assertFalse(delete_saved_filter(conn, 200, saved["id"]))
            self.assertTrue(delete_saved_filter(conn, 100, saved["id"]))

        with connect(self.db) as conn:
            self.assertEqual(list_saved_filters(conn, 100), [])
            self.assertEqual(len(list_saved_filters(conn, 200)), 1)

    def test_saved_filter_duplicate_updates_in_place_and_limit_is_per_chat(self):
        with connect(self.db) as conn:
            first = save_filter(
                conn, 100, "Diário", source="ous", category="all"
            )
            duplicate = save_filter(
                conn,
                100,
                "diário",
                source="ous",
                category="tenis",
                max_price=250,
            )
            self.assertEqual(duplicate["id"], first["id"])
            self.assertEqual(duplicate["category"], "tenis")
            self.assertEqual(len(list_saved_filters(conn, 100)), 1)

            for number in range(2, 11):
                save_filter(
                    conn,
                    100,
                    f"Filtro {number}",
                    source="ous",
                    category="all",
                )
            with self.assertRaisesRegex(ValueError, "limite de 10"):
                save_filter(
                    conn, 100, "Filtro 11", source="ous", category="all"
                )

            # O limite é por chat e atualizar um nome existente continua permitido.
            updated = save_filter(
                conn, 100, "Diário", source="adidas", category="camisetas"
            )
            self.assertEqual(updated["id"], first["id"])
            save_filter(conn, 200, "Primeiro", source="ous", category="all")

    def test_product_ref_is_short_safe_deterministic_and_resolvable(self):
        with connect(self.db) as conn:
            ref = get_or_create_product_ref(conn, "ous", "SKU / ç 123")
            self.assertLessEqual(len(ref), 43)
            self.assertGreaterEqual(len(ref), 12)
            self.assertRegex(ref, re.compile(r"^[A-Za-z0-9_-]+$"))
            self.assertEqual(get_or_create_product_ref(conn, "ous", "SKU / ç 123"), ref)
            self.assertNotEqual(get_or_create_product_ref(conn, "ous", "outro"), ref)

        with connect(self.db) as conn:
            self.assertEqual(get_or_create_product_ref(conn, "ous", "SKU / ç 123"), ref)
            resolved = resolve_product_ref(conn, ref)
            self.assertEqual((resolved["source"], resolved["sku"]), ("ous", "SKU / ç 123"))
            self.assertIsNone(resolve_product_ref(conn, "ref-inexistente"))

    def test_favorite_toggle_isolated_by_chat_and_joins_latest_observation(self):
        with connect(self.db) as conn:
            with patch("ous_monitor.storage._now", return_value="2026-01-01T00:00:00+00:00"):
                record_run(conn, [product(price=100)])
            with patch("ous_monitor.storage._now", return_value="2026-01-02T00:00:00+00:00"):
                record_run(conn, [product(price=80)])

            self.assertTrue(toggle_favorite(conn, 100, "ous", "sku-1"))
            self.assertTrue(toggle_favorite(conn, 200, "ous", "sku-1"))
            self.assertEqual(len(list_favorites(conn, 100)), 1)

        with connect(self.db) as conn:
            favorite = list_favorites(conn, 100)[0]
            self.assertEqual(favorite["name"], "Tênis sku-1")
            self.assertEqual(favorite["price"], 80.0)
            self.assertEqual(favorite["observed_at"], "2026-01-02T00:00:00+00:00")
            self.assertTrue(favorite["ref"])
            self.assertFalse(toggle_favorite(conn, 100, "ous", "sku-1"))
            self.assertTrue(toggle_favorite(conn, 100, "ous", "sku-1"))
            self.assertTrue(delete_favorite(conn, 100, "ous", "sku-1"))
            self.assertFalse(delete_favorite(conn, 100, "ous", "sku-1"))
            self.assertEqual(list_favorites(conn, 100), [])
            self.assertEqual(len(list_favorites(conn, 200)), 1)

    def test_toggle_rejects_unknown_product(self):
        with connect(self.db) as conn:
            with self.assertRaisesRegex(ValueError, "produto não encontrado"):
                toggle_favorite(conn, 100, "ous", "missing")

    def test_alert_preferences_persist_validate_and_list_enabled(self):
        with connect(self.db) as conn:
            self.assertEqual(
                get_alert_preferences(conn, 100),
                {"chat_id": "100", "enabled": False, "hour_utc": 12},
            )
            set_alert_preferences(conn, 100, enabled=True, hour_utc=12)
            set_alert_preferences(conn, 200, enabled=True, hour_utc=21)
            set_alert_preferences(conn, 300, enabled=False, hour_utc=12)
            with self.assertRaisesRegex(ValueError, "entre 0 e 23"):
                set_alert_preferences(conn, 400, enabled=True, hour_utc=24)

        with connect(self.db) as conn:
            self.assertEqual(
                get_alert_preferences(conn, 100),
                {"chat_id": "100", "enabled": True, "hour_utc": 12},
            )
            self.assertEqual(
                [item["chat_id"] for item in list_enabled_alert_preferences(conn)],
                ["100", "200"],
            )
            self.assertEqual(
                [item["chat_id"] for item in list_enabled_alert_preferences(conn, 12)],
                ["100"],
            )
            self.assertEqual(list_enabled_alert_preferences(conn, 0), [])

    def test_personalized_delivery_is_idempotent_per_chat_and_persistent(self):
        event = "price_drop:ous:sku-1:2026-01-02T00:00:00+00:00"
        with connect(self.db) as conn:
            self.assertFalse(was_personalized_alert_delivered(conn, 100, event))
            self.assertTrue(record_personalized_alert_delivery(conn, 100, event))
            self.assertFalse(record_personalized_alert_delivery(conn, 100, event))
            self.assertTrue(record_personalized_alert_delivery(conn, 200, event))

        with connect(self.db) as conn:
            self.assertTrue(was_personalized_alert_delivered(conn, 100, event))
            self.assertTrue(was_personalized_alert_delivered(conn, 200, event))
            self.assertFalse(
                was_personalized_alert_delivered(conn, 100, event + ":other")
            )


if __name__ == "__main__":
    unittest.main()
