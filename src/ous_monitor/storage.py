from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import uuid
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
    last_seen_run_id TEXT,
    PRIMARY KEY (source, sku)
);

CREATE TABLE IF NOT EXISTS price_history (
    source       TEXT NOT NULL,
    sku          TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    run_id       TEXT,
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

CREATE TABLE IF NOT EXISTS runs (
    id                TEXT PRIMARY KEY,
    mode              TEXT NOT NULL,
    requested_sources TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS source_runs (
    run_id       TEXT NOT NULL,
    source       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    raw_count    INTEGER NOT NULL DEFAULT 0,
    kept_count   INTEGER NOT NULL DEFAULT 0,
    drop_gender  INTEGER NOT NULL DEFAULT 0,
    drop_size    INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    PRIMARY KEY (run_id, source),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_source_runs_source_started
    ON source_runs(source, started_at DESC);

CREATE TABLE IF NOT EXISTS bot_sessions (
    chat_id       TEXT PRIMARY KEY,
    state_json    TEXT NOT NULL,
    ui_message_id INTEGER,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_filters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        TEXT NOT NULL,
    name           TEXT NOT NULL COLLATE NOCASE,
    source         TEXT NOT NULL,
    category       TEXT NOT NULL,
    max_price      REAL,
    min_discount   REAL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (chat_id, name)
);

CREATE INDEX IF NOT EXISTS idx_saved_filters_chat_updated
    ON saved_filters(chat_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS product_refs (
    ref          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    sku          TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (source, sku)
);

CREATE TABLE IF NOT EXISTS favorites (
    chat_id      TEXT NOT NULL,
    source       TEXT NOT NULL,
    sku          TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (chat_id, source, sku),
    FOREIGN KEY (source, sku) REFERENCES products(source, sku) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_favorites_chat_created
    ON favorites(chat_id, created_at DESC);

CREATE TABLE IF NOT EXISTS alert_preferences (
    chat_id      TEXT PRIMARY KEY,
    enabled      INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    hour_utc     INTEGER NOT NULL CHECK (hour_utc BETWEEN 0 AND 23),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_preferences_dispatch
    ON alert_preferences(enabled, hour_utc);

CREATE TABLE IF NOT EXISTS personalized_alert_deliveries (
    chat_id      TEXT NOT NULL,
    event_key    TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, event_key)
);

CREATE TABLE IF NOT EXISTS scheduler_state (
    job        TEXT PRIMARY KEY,
    slot       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Adiciona colunas novas em DBs antigos (idempotente)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(price_history)")}
    if "sizes" not in cols:
        conn.execute("ALTER TABLE price_history ADD COLUMN sizes TEXT")
    if "stock_qty" not in cols:
        conn.execute("ALTER TABLE price_history ADD COLUMN stock_qty INTEGER")
    if "run_id" not in cols:
        conn.execute("ALTER TABLE price_history ADD COLUMN run_id TEXT")
    product_cols = {row["name"] for row in conn.execute("PRAGMA table_info(products)")}
    if "last_seen_run_id" not in product_cols:
        conn.execute("ALTER TABLE products ADD COLUMN last_seen_run_id TEXT")
    # DBs anteriores já têm `last_seen`, mas não o vínculo explícito ao run.
    # Prefira o sucesso que precede a última visualização; o segundo SELECT é
    # fallback para bases antigas cujos timestamps não permitem essa correlação.
    conn.execute(
        """
        UPDATE products
           SET last_seen_run_id = COALESCE(
               (
                   SELECT sr.run_id
                     FROM source_runs sr
                     JOIN runs r ON r.id = sr.run_id
                    WHERE sr.source = products.source
                      AND sr.status = 'success'
                      AND sr.finished_at IS NOT NULL
                      AND r.finished_at IS NOT NULL
                      AND sr.finished_at <= products.last_seen
                    ORDER BY sr.finished_at DESC, sr.started_at DESC
                    LIMIT 1
               ),
               (
                   SELECT sr.run_id
                     FROM source_runs sr
                     JOIN runs r ON r.id = sr.run_id
                    WHERE sr.source = products.source
                      AND sr.status = 'success'
                      AND sr.finished_at IS NOT NULL
                      AND r.finished_at IS NOT NULL
                    ORDER BY sr.finished_at DESC, sr.started_at DESC
                    LIMIT 1
               )
           )
         WHERE last_seen_run_id IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_products_source_last_run
            ON products(source, last_seen_run_id)
        """
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@contextmanager
def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if str(db_path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_scheduler_slot(conn: sqlite3.Connection, job: str) -> str | None:
    row = conn.execute(
        "SELECT slot FROM scheduler_state WHERE job = ?",
        (job,),
    ).fetchone()
    return None if row is None else str(row["slot"])


def set_scheduler_slot(conn: sqlite3.Connection, job: str, slot: str) -> None:
    conn.execute(
        """
        INSERT INTO scheduler_state(job, slot, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(job) DO UPDATE SET
            slot = excluded.slot,
            updated_at = excluded.updated_at
        """,
        (job, slot, _now()),
    )


def load_bot_session(conn: sqlite3.Connection, chat_id: int | str) -> dict | None:
    row = conn.execute(
        "SELECT state_json, ui_message_id FROM bot_sessions WHERE chat_id = ?",
        (str(chat_id),),
    ).fetchone()
    if row is None:
        return None
    try:
        state = json.loads(row["state_json"])
    except (TypeError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state["ui_message_id"] = row["ui_message_id"]
    return state


def save_bot_session(
    conn: sqlite3.Connection,
    chat_id: int | str,
    state: dict,
    *,
    ui_message_id: int | None = None,
) -> None:
    payload = dict(state)
    payload.pop("ui_message_id", None)
    conn.execute(
        """
        INSERT INTO bot_sessions(chat_id, state_json, ui_message_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            state_json = excluded.state_json,
            ui_message_id = COALESCE(excluded.ui_message_id, bot_sessions.ui_message_id),
            updated_at = excluded.updated_at
        """,
        (str(chat_id), json.dumps(payload, ensure_ascii=False), ui_message_id, _now()),
    )


def clear_bot_session(conn: sqlite3.Connection, chat_id: int | str) -> None:
    conn.execute("DELETE FROM bot_sessions WHERE chat_id = ?", (str(chat_id),))


SAVED_FILTER_LIMIT = 10
DEFAULT_ALERT_ENABLED = False
DEFAULT_ALERT_HOUR_UTC = 12


def _required_text(value: object, field: str, max_length: int = 200) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise ValueError(f"{field} não pode ser vazio")
    if len(cleaned) > max_length:
        raise ValueError(f"{field} deve ter no máximo {max_length} caracteres")
    return cleaned


def _optional_number(value: object, field: str, maximum: float | None = None) -> float | None:
    if value is None or value == "" or str(value).lower() == "all":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} deve ser numérico") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} deve ser um número finito")
    if number < 0 or (maximum is not None and number > maximum):
        suffix = f" entre 0 e {maximum:g}" if maximum is not None else " positivo"
        raise ValueError(f"{field} deve ser{suffix}")
    return number


def save_filter(
    conn: sqlite3.Connection,
    chat_id: int | str,
    name: str,
    *,
    source: str,
    category: str,
    max_price: object = None,
    min_discount: object = None,
) -> sqlite3.Row:
    """Cria ou atualiza um filtro pelo nome, limitado a 10 por chat."""
    chat = _required_text(chat_id, "chat_id", 100)
    clean_name = _required_text(name, "name", 60)
    clean_source = _required_text(source, "source", 100)
    clean_category = _required_text(category, "category", 100)
    clean_max_price = _optional_number(max_price, "max_price")
    clean_min_discount = _optional_number(min_discount, "min_discount", 100)

    existing = conn.execute(
        "SELECT id FROM saved_filters WHERE chat_id = ? AND name = ? COLLATE NOCASE",
        (chat, clean_name),
    ).fetchone()
    now = _now()
    if existing is None:
        count = conn.execute(
            "SELECT COUNT(*) FROM saved_filters WHERE chat_id = ?", (chat,)
        ).fetchone()[0]
        if count >= SAVED_FILTER_LIMIT:
            raise ValueError(f"limite de {SAVED_FILTER_LIMIT} filtros salvos atingido")
        cursor = conn.execute(
            """
            INSERT INTO saved_filters(
                chat_id, name, source, category, max_price, min_discount,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat, clean_name, clean_source, clean_category, clean_max_price,
                clean_min_discount, now, now,
            ),
        )
        filter_id = cursor.lastrowid
    else:
        filter_id = existing["id"]
        conn.execute(
            """
            UPDATE saved_filters
               SET name = ?, source = ?, category = ?, max_price = ?,
                   min_discount = ?, updated_at = ?
             WHERE chat_id = ? AND id = ?
            """,
            (
                clean_name, clean_source, clean_category, clean_max_price,
                clean_min_discount, now, chat, filter_id,
            ),
        )
    return conn.execute(
        "SELECT * FROM saved_filters WHERE chat_id = ? AND id = ?",
        (chat, filter_id),
    ).fetchone()


def list_saved_filters(conn: sqlite3.Connection, chat_id: int | str) -> list:
    return list(conn.execute(
        """
        SELECT * FROM saved_filters
         WHERE chat_id = ?
         ORDER BY updated_at DESC, id DESC
        """,
        (str(chat_id),),
    ))


def load_saved_filter(
    conn: sqlite3.Connection, chat_id: int | str, filter_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM saved_filters WHERE chat_id = ? AND id = ?",
        (str(chat_id), int(filter_id)),
    ).fetchone()


def delete_saved_filter(
    conn: sqlite3.Connection, chat_id: int | str, filter_id: int
) -> bool:
    cursor = conn.execute(
        "DELETE FROM saved_filters WHERE chat_id = ? AND id = ?",
        (str(chat_id), int(filter_id)),
    )
    return cursor.rowcount == 1


def _product_ref_digest(source: str, sku: str) -> str:
    payload = f"{len(source)}:{source}{sku}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def get_or_create_product_ref(
    conn: sqlite3.Connection, source: str, sku: str
) -> str:
    """Retorna uma ref estável, opaca e segura para usar em callback_data."""
    clean_source = _required_text(source, "source", 200)
    clean_sku = _required_text(sku, "sku", 500)
    row = conn.execute(
        "SELECT ref FROM product_refs WHERE source = ? AND sku = ?",
        (clean_source, clean_sku),
    ).fetchone()
    if row is not None:
        return row["ref"]

    digest = _product_ref_digest(clean_source, clean_sku)
    lengths = list(range(12, len(digest), 2)) + [len(digest)]
    for length in lengths:
        ref = digest[:length]
        collision = conn.execute(
            "SELECT source, sku FROM product_refs WHERE ref = ?", (ref,)
        ).fetchone()
        if collision is not None:
            if collision["source"] == clean_source and collision["sku"] == clean_sku:
                return ref
            continue
        conn.execute(
            "INSERT OR IGNORE INTO product_refs(ref, source, sku, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ref, clean_source, clean_sku, _now()),
        )
        inserted = conn.execute(
            "SELECT source, sku FROM product_refs WHERE ref = ?", (ref,)
        ).fetchone()
        if (
            inserted is not None
            and inserted["source"] == clean_source
            and inserted["sku"] == clean_sku
        ):
            return ref
        concurrent = conn.execute(
            "SELECT ref FROM product_refs WHERE source = ? AND sku = ?",
            (clean_source, clean_sku),
        ).fetchone()
        if concurrent is not None:
            return concurrent["ref"]
    raise RuntimeError("não foi possível gerar uma referência de produto única")


def resolve_product_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT source, sku FROM product_refs WHERE ref = ?",
        (_required_text(ref, "ref", 50),),
    ).fetchone()


def toggle_favorite(
    conn: sqlite3.Connection, chat_id: int | str, source: str, sku: str
) -> bool:
    """Alterna o favorito e retorna True quando o produto ficou favoritado."""
    chat = _required_text(chat_id, "chat_id", 100)
    clean_source = _required_text(source, "source", 200)
    clean_sku = _required_text(sku, "sku", 500)
    product = conn.execute(
        "SELECT 1 FROM products WHERE source = ? AND sku = ?",
        (clean_source, clean_sku),
    ).fetchone()
    if product is None:
        raise ValueError("produto não encontrado")
    get_or_create_product_ref(conn, clean_source, clean_sku)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO favorites(chat_id, source, sku, created_at) "
        "VALUES (?, ?, ?, ?)",
        (chat, clean_source, clean_sku, _now()),
    )
    if cursor.rowcount == 1:
        return True
    conn.execute(
        "DELETE FROM favorites WHERE chat_id = ? AND source = ? AND sku = ?",
        (chat, clean_source, clean_sku),
    )
    return False


def delete_favorite(
    conn: sqlite3.Connection, chat_id: int | str, source: str, sku: str
) -> bool:
    """Remove um favorito de forma idempotente."""
    cursor = conn.execute(
        "DELETE FROM favorites WHERE chat_id = ? AND source = ? AND sku = ?",
        (str(chat_id), str(source), str(sku)),
    )
    return cursor.rowcount == 1


def list_favorites(conn: sqlite3.Connection, chat_id: int | str) -> list:
    """Lista favoritos com cadastro, ref curta e a observação mais recente."""
    return list(conn.execute(
        """
        SELECT f.chat_id, f.created_at AS favorited_at,
               p.source, p.sku, pr.ref, p.name, p.url, p.image, p.brand,
               ph.list_price, ph.price, ph.available, ph.sizes, ph.stock_qty,
               ph.observed_at
          FROM favorites f
          JOIN products p
            ON p.source = f.source AND p.sku = f.sku
          JOIN product_refs pr
            ON pr.source = f.source AND pr.sku = f.sku
          LEFT JOIN price_history ph
            ON ph.source = f.source
           AND ph.sku = f.sku
           AND ph.observed_at = (
               SELECT MAX(latest.observed_at)
                 FROM price_history latest
                WHERE latest.source = f.source AND latest.sku = f.sku
           )
         WHERE f.chat_id = ?
         ORDER BY f.created_at DESC, p.source, p.name
        """,
        (str(chat_id),),
    ))


def _alert_preferences_dict(chat_id: int | str, enabled: bool, hour_utc: int) -> dict:
    return {
        "chat_id": str(chat_id),
        "enabled": bool(enabled),
        "hour_utc": int(hour_utc),
    }


def get_alert_preferences(conn: sqlite3.Connection, chat_id: int | str) -> dict:
    row = conn.execute(
        "SELECT enabled, hour_utc FROM alert_preferences WHERE chat_id = ?",
        (str(chat_id),),
    ).fetchone()
    if row is None:
        return _alert_preferences_dict(
            chat_id, DEFAULT_ALERT_ENABLED, DEFAULT_ALERT_HOUR_UTC
        )
    return _alert_preferences_dict(chat_id, row["enabled"], row["hour_utc"])


def set_alert_preferences(
    conn: sqlite3.Connection,
    chat_id: int | str,
    *,
    enabled: bool,
    hour_utc: int,
) -> dict:
    chat = _required_text(chat_id, "chat_id", 100)
    if not isinstance(enabled, bool):
        raise ValueError("enabled deve ser booleano")
    if isinstance(hour_utc, bool) or not isinstance(hour_utc, int):
        raise ValueError("hour_utc deve ser um inteiro entre 0 e 23")
    if not 0 <= hour_utc <= 23:
        raise ValueError("hour_utc deve estar entre 0 e 23")
    now = _now()
    conn.execute(
        """
        INSERT INTO alert_preferences(chat_id, enabled, hour_utc, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            enabled = excluded.enabled,
            hour_utc = excluded.hour_utc,
            updated_at = excluded.updated_at
        """,
        (chat, int(enabled), hour_utc, now, now),
    )
    return _alert_preferences_dict(chat, enabled, hour_utc)


def list_enabled_alert_preferences(
    conn: sqlite3.Connection, hour_utc: int | None = None
) -> list:
    params = []
    where = "enabled = 1"
    if hour_utc is not None:
        if (
            isinstance(hour_utc, bool)
            or not isinstance(hour_utc, int)
            or not 0 <= hour_utc <= 23
        ):
            raise ValueError("hour_utc deve estar entre 0 e 23")
        where += " AND hour_utc = ?"
        params.append(hour_utc)
    rows = conn.execute(
        f"SELECT chat_id, enabled, hour_utc FROM alert_preferences WHERE {where} "
        "ORDER BY chat_id",
        params,
    )
    return [
        _alert_preferences_dict(row["chat_id"], row["enabled"], row["hour_utc"])
        for row in rows
    ]


def was_personalized_alert_delivered(
    conn: sqlite3.Connection, chat_id: int | str, event_key: str
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM personalized_alert_deliveries
         WHERE chat_id = ? AND event_key = ?
        """,
        (str(chat_id), _required_text(event_key, "event_key", 500)),
    ).fetchone()
    return row is not None


def record_personalized_alert_delivery(
    conn: sqlite3.Connection, chat_id: int | str, event_key: str
) -> bool:
    """Registra entrega; retorna False quando o evento já havia sido entregue."""
    chat = _required_text(chat_id, "chat_id", 100)
    event = _required_text(event_key, "event_key", 500)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO personalized_alert_deliveries(
            chat_id, event_key, delivered_at
        ) VALUES (?, ?, ?)
        """,
        (chat, event, _now()),
    )
    return cursor.rowcount == 1


def start_run(conn: sqlite3.Connection, *, mode: str, sources: Iterable[str]) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO runs(id, mode, requested_sources, started_at, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, mode, ",".join(sources), _now(), "running"),
    )
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs
           SET finished_at = ?, status = ?, error = ?
         WHERE id = ?
        """,
        (_now(), status, error, run_id),
    )


def record_source_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source: str,
    started_at: str,
    status: str,
    raw_count: int = 0,
    kept_count: int = 0,
    drop_gender: int = 0,
    drop_size: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO source_runs(
            run_id, source, started_at, finished_at, status,
            raw_count, kept_count, drop_gender, drop_size, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, source, started_at, _now(), status,
            raw_count, kept_count, drop_gender, drop_size, error,
        ),
    )


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


def record_run(
    conn: sqlite3.Connection,
    products: Iterable[Product],
    *,
    run_id: str | None = None,
) -> dict[str, int]:
    """Persist a run. Returns counters: {'new', 'updated', 'price_drop', 'new_promo'}."""
    now = _now()
    counters = {"new": 0, "updated": 0, "price_drop": 0, "new_promo": 0, "duplicates": 0}

    products_by_key: dict[tuple[str, str], Product] = {}
    for p in products:
        key = (p.source, p.sku)
        if key in products_by_key:
            counters["duplicates"] += 1
        products_by_key[key] = p
    products_list = list(products_by_key.values())

    keys = {(p.source, p.sku) for p in products_list}
    prev_map: dict[tuple[str, str], sqlite3.Row] = {}
    if keys:
        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS _record_run_keys (
                source TEXT NOT NULL,
                sku    TEXT NOT NULL,
                PRIMARY KEY (source, sku)
            )
            """
        )
        conn.execute("DELETE FROM _record_run_keys")
        conn.executemany(
            "INSERT OR IGNORE INTO _record_run_keys(source, sku) VALUES (?, ?)",
            list(keys),
        )
        rows = conn.execute(
            """
            SELECT ph.source, ph.sku, ph.list_price, ph.price, ph.available,
                   ph.sizes, ph.stock_qty, ph.observed_at
              FROM price_history ph
              JOIN _record_run_keys rk
                ON rk.source = ph.source
               AND rk.sku = ph.sku
             ORDER BY ph.observed_at DESC
            """,
        ).fetchall()
        for r in rows:
            key = (r["source"], r["sku"])
            if key not in prev_map:
                prev_map[key] = r
        conn.execute("DELETE FROM _record_run_keys")

    for p in products_list:
        prev = prev_map.get((p.source, p.sku))

        existing = conn.execute(
            "SELECT 1 FROM products WHERE source = ? AND sku = ?",
            (p.source, p.sku),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO products(
                    source, sku, name, url, image, brand, first_seen, last_seen,
                    last_seen_run_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (p.source, p.sku, p.name, p.url, p.image, p.brand, now, now, run_id),
            )
            counters["new"] += 1
        else:
            conn.execute(
                """
                UPDATE products
                   SET name = ?, url = ?, image = ?, brand = ?, last_seen = ?,
                       last_seen_run_id = ?
                 WHERE source = ? AND sku = ?
                """,
                (p.name, p.url, p.image, p.brand, now, run_id, p.source, p.sku),
            )
            counters["updated"] += 1

        sizes = ",".join(p.sizes) if p.sizes else None
        observation_changed = (
            prev is None
            or prev["list_price"] != p.list_price
            or prev["price"] != p.price
            or prev["available"] != int(p.available)
            or prev["sizes"] != sizes
            or prev["stock_qty"] != p.stock_qty
        )
        if observation_changed:
            conn.execute(
                """
                INSERT OR REPLACE INTO price_history
                    (source, sku, observed_at, run_id, list_price, price, available, sizes, stock_qty)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p.source, p.sku, now, run_id, p.list_price, p.price,
                    int(p.available), sizes, p.stock_qty,
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


def latest_source_runs(conn: sqlite3.Connection) -> list:
    return list(conn.execute("""
        WITH latest AS (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY source ORDER BY started_at DESC) AS rn
              FROM source_runs
        )
        SELECT source, run_id, started_at, finished_at, status,
               raw_count, kept_count, drop_gender, drop_size, error
          FROM latest
         WHERE rn = 1
         ORDER BY source
    """))
