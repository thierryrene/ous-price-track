from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ous_monitor.models import Product
from ous_monitor.services import CatalogService
from ous_monitor.storage import connect, find_changes, record_run, snapshot_promotions


def product(sku: str, price: float, list_price: float | None, *, name: str = "Tênis Teste") -> Product:
    return Product(
        source="test",
        sku=sku,
        name=name,
        url=f"https://example.test/{sku}",
        image=None,
        list_price=list_price,
        price=price,
        available=True,
        sizes=["42"],
    )


class StorageServicesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "prices.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_record_run_creates_and_updates_product_history(self) -> None:
        with connect(self.db) as conn:
            with patch("ous_monitor.storage._now", return_value="2026-01-01T00:00:00+00:00"):
                counters = record_run(conn, [product("sku-1", 100, 150)])
            self.assertEqual(counters["new"], 1)
            self.assertEqual(counters["new_promo"], 1)

            with patch("ous_monitor.storage._now", return_value="2026-01-01T00:01:00+00:00"):
                counters = record_run(conn, [product("sku-1", 90, 150, name="Tênis Teste Novo")])
            self.assertEqual(counters["updated"], 1)
            self.assertEqual(counters["price_drop"], 1)

            product_row = conn.execute(
                "SELECT name FROM products WHERE source='test' AND sku='sku-1'"
            ).fetchone()
            history_count = conn.execute(
                "SELECT COUNT(*) FROM price_history WHERE source='test' AND sku='sku-1'"
            ).fetchone()[0]

        self.assertEqual(product_row["name"], "Tênis Teste Novo")
        self.assertEqual(history_count, 2)

    def test_find_changes_classifies_latest_observation_once(self) -> None:
        with connect(self.db) as conn:
            with patch("ous_monitor.storage._now", return_value="2026-01-01T00:00:00+00:00"):
                record_run(conn, [
                    product("new", 100, 100),
                    product("ended", 70, 100),
                    product("weaker", 60, 100),
                    product("up", 100, 100),
                ])
            with patch("ous_monitor.storage._now", return_value="2026-01-01T00:01:00+00:00"):
                record_run(conn, [
                    product("new", 70, 100),
                    product("ended", 100, 100),
                    product("weaker", 85, 100),
                    product("up", 106, 106),
                ])
            changes = find_changes(conn, "2026-01-01T00:00:30+00:00")

        self.assertEqual([r["sku"] for r in changes["new_promo"]], ["new"])
        self.assertEqual([r["sku"] for r in changes["ended"]], ["ended"])
        self.assertEqual([r["sku"] for r in changes["weaker"]], ["weaker"])
        self.assertEqual([r["sku"] for r in changes["price_up"]], ["up"])

    def test_snapshot_and_purge_use_latest_state(self) -> None:
        with connect(self.db) as conn:
            with patch("ous_monitor.storage._now", return_value="2026-01-01T00:00:00+00:00"):
                record_run(conn, [
                    product("promo", 50, 100),
                    product("full", 100, 100),
                    product("bad-size", 80, 100, name="Tênis Feminino Teste"),
                ])
            snapshot = snapshot_promotions(conn)

        self.assertEqual({r["sku"] for r in snapshot["new_promo"]}, {"promo", "bad-size"})

        service = CatalogService(self.db)
        dry = service.purge_candidates()
        self.assertEqual([c.sku for c in dry.candidates], ["bad-size"])

        applied = service.purge_apply()
        self.assertTrue(applied.applied)

        with connect(self.db) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM products WHERE sku='bad-size'"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
