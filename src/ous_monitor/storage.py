from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Product

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    source       TEXT NOT NULL,
    sku          TEXT NOT NULL,
    name         TEXT NOT NULL,
    url          TEXT NOT NULL,
    image        TEXT,
    brand        TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (source, sku)
);

CREATE TABLE IF NOT EXISTS price_history (
    source       TEXT NOT NULL,
    sku          TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    list_price   REAL,
    price        REAL NOT NULL,
    available    INTEGER NOT NULL,
    sizes        TEXT,        -- CSV de tamanhos disponíveis no momento
    stock_qty    INTEGER,     -- soma de estoque ou NULL se fonte não reporta
    PRIMARY KEY (source, sku, observed_at),
    FOREIGN KEY (source, sku) REFERENCES products(source, sku)
);

CREATE INDEX IF NOT EXISTS idx_price_history_lookup
    ON price_history(source, sku, observed_at DESC);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Adiciona colunas novas em DBs antigos (idempotente)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(price_history)")}
    if "sizes" not in cols:
        conn.execute("ALTER TABLE price_history ADD COLUMN sizes TEXT")
    if "stock_qty" not in cols:
        conn.execute("ALTER TABLE price_history ADD COLUMN stock_qty INTEGER")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def latest_observation(conn: sqlite3.Connection, source: str, sku: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT list_price, price, available, observed_at
          FROM price_history
         WHERE source = ? AND sku = ?
         ORDER BY observed_at DESC
         LIMIT 1
        """,
        (source, sku),
    ).fetchone()


def record_run(conn: sqlite3.Connection, products: Iterable[Product]) -> dict[str, int]:
    """Persist a run. Returns counters: {'new', 'updated', 'price_drop', 'new_promo'}."""
    now = _now()
    counters = {"new": 0, "updated": 0, "price_drop": 0, "new_promo": 0}

    for p in products:
        prev = latest_observation(conn, p.source, p.sku)

        existing = conn.execute(
            "SELECT 1 FROM products WHERE source = ? AND sku = ?",
            (p.source, p.sku),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO products(source, sku, name, url, image, brand, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (p.source, p.sku, p.name, p.url, p.image, p.brand, now, now),
            )
            counters["new"] += 1
        else:
            conn.execute(
                """
                UPDATE products
                   SET name = ?, url = ?, image = ?, brand = ?, last_seen = ?
                 WHERE source = ? AND sku = ?
                """,
                (p.name, p.url, p.image, p.brand, now, p.source, p.sku),
            )
            counters["updated"] += 1

        conn.execute(
            """
            INSERT OR REPLACE INTO price_history
                (source, sku, observed_at, list_price, price, available, sizes, stock_qty)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p.source, p.sku, now, p.list_price, p.price, int(p.available),
                ",".join(p.sizes) if p.sizes else None,
                p.stock_qty,
            ),
        )

        if prev is not None:
            if p.price < prev["price"]:
                counters["price_drop"] += 1
            prev_had_discount = (
                prev["list_price"] is not None and prev["list_price"] > prev["price"]
            )
            if p.has_discount and not prev_had_discount:
                counters["new_promo"] += 1
        elif p.has_discount:
            counters["new_promo"] += 1

    return counters


def find_new_promotions(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Products that started a discount after `since` (ISO timestamp)."""
    return list(
        conn.execute(
            """
            WITH ranked AS (
                SELECT source, sku, observed_at, list_price, price,
                       sizes, stock_qty,
                       LAG(price)      OVER w AS prev_price,
                       LAG(list_price) OVER w AS prev_list_price
                  FROM price_history
                 WINDOW w AS (PARTITION BY source, sku ORDER BY observed_at)
            )
            SELECT p.source, p.sku, p.name, p.url, p.image,
                   r.list_price, r.price, r.observed_at,
                   r.prev_price, r.prev_list_price,
                   r.sizes, r.stock_qty
              FROM ranked r
              JOIN products p USING (source, sku)
             WHERE r.observed_at >= ?
               AND r.list_price IS NOT NULL
               AND r.list_price > r.price
               AND (r.prev_price IS NULL OR r.prev_price > r.price)
             ORDER BY r.observed_at DESC, p.source, p.name
            """,
            (since,),
        )
    )
