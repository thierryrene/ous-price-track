from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ous_monitor.models import Product
from ous_monitor.storage import (
    connect,
    find_changes,
    finish_run,
    latest_source_runs,
    record_run,
    record_source_run,
    start_run,
)


def product(price: float, list_price: float | None = None) -> Product:
    return Product(
        source="test",
        sku="sku-1",
        name="Tênis Teste",
        url="https://example.test/p",
        image=None,
        list_price=list_price,
        price=price,
        available=True,
        brand="Teste",
        sizes=["42"],
    )


class StorageTests(unittest.TestCase):
    def test_record_run_deduplicates_products_and_links_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            with connect(db) as conn:
                run_id = start_run(conn, mode="alert", sources=["test"])
                counters = record_run(conn, [product(100), product(90, 120)], run_id=run_id)
                finish_run(conn, run_id, status="success")

                self.assertEqual(counters["duplicates"], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)
                row = conn.execute("SELECT run_id, price FROM price_history").fetchone()
                self.assertEqual(row["run_id"], run_id)
                self.assertEqual(row["price"], 90)

    def test_source_run_status_is_queryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            with connect(db) as conn:
                run_id = start_run(conn, mode="alert", sources=["test"])
                record_source_run(
                    conn,
                    run_id=run_id,
                    source="test",
                    started_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    status="success",
                    raw_count=2,
                    kept_count=1,
                    drop_size=1,
                )
                finish_run(conn, run_id, status="success")
                rows = latest_source_runs(conn)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "test")
            self.assertEqual(rows[0]["kept_count"], 1)

    def test_find_changes_detects_new_promo(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            with connect(db) as conn:
                run_id = start_run(conn, mode="alert", sources=["test"])
                record_run(conn, [product(100, 100)], run_id=run_id)
                finish_run(conn, run_id, status="success")

            time.sleep(0.01)
            since = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            time.sleep(0.01)

            with connect(db) as conn:
                run_id = start_run(conn, mode="alert", sources=["test"])
                record_run(conn, [product(80, 100)], run_id=run_id)
                finish_run(conn, run_id, status="success")
                changes = find_changes(conn, since)

            self.assertEqual(len(changes["new_promo"]), 1)
            self.assertEqual(changes["new_promo"][0]["sku"], "sku-1")


if __name__ == "__main__":
    unittest.main()
