from __future__ import annotations

import sqlite3
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
    load_bot_session,
    record_run,
    record_source_run,
    recover_interrupted_runs,
    save_bot_session,
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
    def test_recover_interrupted_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            with connect(db) as conn:
                run_id = start_run(conn, mode="alert", sources=["test"])
                self.assertEqual(recover_interrupted_runs(conn), 1)
                row = conn.execute(
                    "SELECT status, finished_at, error FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertIsNotNone(row["finished_at"])
                self.assertIn("restart", row["error"])
                self.assertEqual(recover_interrupted_runs(conn), 0)

    def test_bot_session_survives_new_database_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            state = {
                "source": "ous",
                "category": "tenis",
                "max_price": "200",
                "min_discount": "30",
            }
            with connect(db) as conn:
                save_bot_session(conn, 123, state, ui_message_id=77)

            with connect(db) as conn:
                restored = load_bot_session(conn, "123")

            self.assertEqual(restored["source"], "ous")
            self.assertEqual(restored["category"], "tenis")
            self.assertEqual(restored["ui_message_id"], 77)

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
                seen_run = conn.execute(
                    "SELECT last_seen_run_id FROM products"
                ).fetchone()[0]
                self.assertEqual(seen_run, run_id)

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

    def test_record_run_skips_unchanged_history_but_updates_last_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            with connect(db) as conn:
                record_run(conn, [product(90, 120)])
                first_seen = conn.execute(
                    "SELECT last_seen FROM products WHERE source='test' AND sku='sku-1'"
                ).fetchone()["last_seen"]

            time.sleep(0.01)
            with connect(db) as conn:
                record_run(conn, [product(90, 120)])
                row = conn.execute(
                    "SELECT last_seen FROM products WHERE source='test' AND sku='sku-1'"
                ).fetchone()
                observations = conn.execute(
                    "SELECT COUNT(*) FROM price_history"
                ).fetchone()[0]

            self.assertGreater(row["last_seen"], first_seen)
            self.assertEqual(observations, 1)

    def test_migration_backfills_last_seen_run_id_from_prior_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE products (
                    source TEXT NOT NULL, sku TEXT NOT NULL, name TEXT NOT NULL,
                    url TEXT NOT NULL, image TEXT, brand TEXT,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY (source, sku)
                );
                CREATE TABLE price_history (
                    source TEXT NOT NULL, sku TEXT NOT NULL, observed_at TEXT NOT NULL,
                    list_price REAL, price REAL NOT NULL, available INTEGER NOT NULL,
                    PRIMARY KEY (source, sku, observed_at)
                );
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, requested_sources TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, error TEXT
                );
                CREATE TABLE source_runs (
                    run_id TEXT NOT NULL, source TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT, status TEXT NOT NULL, raw_count INTEGER NOT NULL DEFAULT 0,
                    kept_count INTEGER NOT NULL DEFAULT 0, drop_gender INTEGER NOT NULL DEFAULT 0,
                    drop_size INTEGER NOT NULL DEFAULT 0, error TEXT,
                    PRIMARY KEY (run_id, source)
                );
                INSERT INTO products VALUES (
                    'test', 'sku-1', 'Tênis', 'https://example.test', NULL, NULL,
                    '2026-01-01T00:00:00+00:00', '2026-01-01T02:00:00+00:00'
                );
                INSERT INTO products VALUES (
                    'test', 'sku-fallback', 'Tênis', 'https://example.test', NULL, NULL,
                    '2025-12-01T00:00:00+00:00', '2025-12-01T00:00:00+00:00'
                );
                INSERT INTO runs VALUES (
                    'run-ok', 'snapshot', 'test', '2026-01-01T00:00:00+00:00',
                    '2026-01-01T01:00:00+00:00', 'success', NULL
                );
                INSERT INTO source_runs(run_id, source, started_at, finished_at, status)
                VALUES ('run-ok', 'test', '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:30:00+00:00', 'success');
            """)
            conn.commit()
            conn.close()

            with connect(db) as migrated:
                rows = list(migrated.execute(
                    "SELECT last_seen_run_id FROM products ORDER BY sku"
                ))

            self.assertEqual([row["last_seen_run_id"] for row in rows], [
                "run-ok", "run-ok",
            ])

    def test_record_run_keeps_real_observation_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "prices.db"
            with connect(db) as conn:
                record_run(conn, [product(90, 120)])

            time.sleep(0.01)
            with connect(db) as conn:
                record_run(conn, [product(80, 120)])
                prices = [
                    row["price"]
                    for row in conn.execute(
                        "SELECT price FROM price_history ORDER BY observed_at"
                    )
                ]

            self.assertEqual(prices, [90, 80])

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
