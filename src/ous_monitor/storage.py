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


# Thresholds (moderados, conforme decisão do usuário em 2026-05-09)
PRICE_UP_RATIO = 0.05         # +5% no preço dispara "subiu"
DISCOUNT_SHRINK_RATIO = 0.25  # desconto % encolheu em 25%+ relativo dispara "enfraqueceu"


def _ranked_with_prev(since: str) -> str:
    """SQL fragment: para cada SKU, retorna SUA observação MAIS RECENTE (se ela
    cair dentro da janela `since`) com a observação imediatamente anterior do
    mesmo SKU como `prev_*`.

    Importante: queremos no máximo 1 linha por SKU. Sem isso, executar com
    janela de 24h numa série temporal de 4 snapshots faria o produto disparar
    em 3 linhas, multiplicando notificações.
    """
    return """
        WITH ranked AS (
            SELECT source, sku, observed_at, list_price, price, sizes, stock_qty,
                   LAG(price)      OVER w AS prev_price,
                   LAG(list_price) OVER w AS prev_list_price,
                   LAG(observed_at) OVER w AS prev_observed_at,
                   ROW_NUMBER() OVER w_desc AS rn
              FROM price_history
             WINDOW w      AS (PARTITION BY source, sku ORDER BY observed_at),
                    w_desc AS (PARTITION BY source, sku ORDER BY observed_at DESC)
        )
        SELECT p.source, p.sku, p.name, p.url, p.image,
               r.list_price, r.price, r.observed_at,
               r.prev_price, r.prev_list_price, r.prev_observed_at,
               r.sizes, r.stock_qty
          FROM ranked r
          JOIN products p USING (source, sku)
         WHERE r.rn = 1            -- só a observação mais recente do SKU
           AND r.observed_at >= ?  -- e ela precisa ter caído na janela
    """


def find_changes(conn: sqlite3.Connection, since: str) -> dict:
    """Detecta 4 categorias de mudança desde `since`:

    - 'new_promo': produto começou um desconto (ou caiu mais)
    - 'price_up': preço subiu ≥5% (e não acabou — está coberto em 'ended')
    - 'ended':    promo acabou (price agora == list_price; antes price < list_price)
    - 'weaker':   promo enfraqueceu (desconto % encolheu ≥25% relativo)

    Retorna dict[str, list[sqlite3.Row]]. Categorias são mutuamente exclusivas
    pra cada SKU dentro do mesmo run (priorização: new_promo > ended > weaker > price_up).
    """
    base = _ranked_with_prev(since)

    new_promo = list(conn.execute(
        base + """
           AND r.list_price IS NOT NULL
           AND r.list_price > r.price
           AND (r.prev_price IS NULL OR r.prev_price > r.price)
         ORDER BY r.observed_at DESC, p.source, p.name
        """,
        (since,),
    ))

    ended = list(conn.execute(
        base + """
           AND r.list_price IS NOT NULL
           AND r.price >= r.list_price          -- está a preço cheio agora
           AND r.prev_price IS NOT NULL
           AND r.prev_list_price IS NOT NULL
           AND r.prev_price < r.prev_list_price -- estava em promo antes
         ORDER BY r.observed_at DESC, p.source, p.name
        """,
        (since,),
    ))

    # IDs já cobertos por categorias mais prioritárias — evitar dupla contagem.
    covered = {(r["source"], r["sku"]) for r in new_promo}
    covered.update((r["source"], r["sku"]) for r in ended)

    weaker_raw = list(conn.execute(
        base + """
           AND r.list_price IS NOT NULL
           AND r.list_price > r.price             -- ainda em promo
           AND r.prev_price IS NOT NULL
           AND r.prev_list_price IS NOT NULL
           AND r.prev_price < r.prev_list_price   -- estava em promo antes
         ORDER BY r.observed_at DESC, p.source, p.name
        """,
        (since,),
    ))
    weaker = []
    for r in weaker_raw:
        if (r["source"], r["sku"]) in covered:
            continue
        prev_disc = 1 - (r["prev_price"] / r["prev_list_price"])
        cur_disc = 1 - (r["price"] / r["list_price"])
        if prev_disc <= 0:
            continue
        rel_shrink = (prev_disc - cur_disc) / prev_disc
        if rel_shrink >= DISCOUNT_SHRINK_RATIO:
            weaker.append(r)
    covered.update((r["source"], r["sku"]) for r in weaker)

    price_up_raw = list(conn.execute(
        base + """
           AND r.prev_price IS NOT NULL
           AND r.price > r.prev_price * (1 + ?)
         ORDER BY r.observed_at DESC, p.source, p.name
        """,
        (since, PRICE_UP_RATIO),
    ))
    price_up = [r for r in price_up_raw if (r["source"], r["sku"]) not in covered]

    return {
        "new_promo": new_promo,
        "ended": ended,
        "weaker": weaker,
        "price_up": price_up,
    }


def find_new_promotions(conn: sqlite3.Connection, since: str) -> list:
    """Backwards-compatible wrapper — retorna só a categoria new_promo."""
    return find_changes(conn, since)["new_promo"]


def snapshot_promotions(conn: sqlite3.Connection) -> dict:
    """Retorna TODOS os produtos atualmente em promoção (último snapshot por SKU),
    no mesmo formato de `find_changes` — todos sob a categoria 'new_promo'.

    Diferente de find_changes: ignora a janela temporal e o estado anterior.
    Pensado pro subcomando `snapshot`, que dá o panorama completo do dia
    independentemente de "já foi notificado".

    As linhas têm prev_price=NULL (compatível com formatador) e mantêm os
    mesmos nomes de colunas que o resto do pipeline espera.
    """
    rows = list(conn.execute("""
        WITH latest AS (
            SELECT source, sku, list_price, price, sizes, stock_qty, observed_at,
                   ROW_NUMBER() OVER (PARTITION BY source, sku
                                      ORDER BY observed_at DESC) AS rn
              FROM price_history
        )
        SELECT p.source, p.sku, p.name, p.url, p.image,
               l.list_price, l.price, l.observed_at,
               NULL AS prev_price,
               NULL AS prev_list_price,
               NULL AS prev_observed_at,
               l.sizes, l.stock_qty
          FROM latest l
          JOIN products p USING (source, sku)
         WHERE l.rn = 1
           AND l.list_price IS NOT NULL
           AND l.list_price > l.price
         ORDER BY (1.0 - l.price / l.list_price) DESC, p.source, p.name
    """))
    return {"new_promo": rows, "ended": [], "weaker": [], "price_up": []}
