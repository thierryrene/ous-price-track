from __future__ import annotations

import fcntl
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from threading import Lock
from typing import Iterable

from .filters import should_keep, should_keep_product
from .models import Product, RunCounters
from .storage import (
    connect, finish_run, find_changes, record_run, record_source_run,
    snapshot_promotions, start_run,
)

log = logging.getLogger(__name__)


CHANGE_CATEGORIES = ("new_promo", "ended", "weaker", "price_up")


@dataclass(frozen=True)
class ScrapeRunResult:
    products: list[Product]
    failed: list[str]
    counters: RunCounters

    @property
    def ok(self) -> bool:
        return bool(self.products) or not self.failed


@dataclass(frozen=True)
class MonitorResult:
    scrape: ScrapeRunResult
    changes: dict
    mode: str
    cutoff_iso: str

    @property
    def total_changes(self) -> int:
        return sum(len(self.changes.get(k, [])) for k in CHANGE_CATEGORIES)


@dataclass(frozen=True)
class SnapshotResult:
    scrape: ScrapeRunResult
    changes: dict

    @property
    def total_promotions(self) -> int:
        return len(self.changes.get("new_promo", []))


@dataclass(frozen=True)
class PurgeCandidate:
    source: str
    sku: str
    name: str
    reason: str


@dataclass(frozen=True)
class PurgeResult:
    candidates: list[PurgeCandidate]
    observations: int = 0
    applied: bool = False


@dataclass(frozen=True)
class NormalizeResult:
    old_observations: int
    stale_products: int
    bad_price_products: int
    removed: int = 0
    applied: bool = False


@dataclass(frozen=True)
class ProductFilters:
    category: str = "all"
    max_price: str = "all"
    min_discount: str = "all"

    @classmethod
    def from_mapping(cls, values: dict | None) -> "ProductFilters":
        values = values or {}
        return cls(
            category=str(values.get("category", "all")),
            max_price=str(values.get("max_price", "all")),
            min_discount=str(values.get("min_discount", "all")),
        )


class SourceRegistry:
    """Registro de fontes. Fonte única de verdade em `sources.SOURCES` (inclui
    umbro e approve); aqui só projetamos chave -> factory de scraper.

    Todos os scrapers são httpx/selectolax (sem Playwright), então importar
    `sources` é leve e seguro pro CI.
    """

    @staticmethod
    def all() -> dict:
        from .sources import SOURCES
        return {key: cfg.scraper_factory for key, cfg in SOURCES.items()}

    @classmethod
    def names(cls) -> list[str]:
        return list(cls.all())


@contextmanager
def monitor_file_lock(db_path: Path, timeout_s: float = 10.0):
    """Lock de arquivo (fcntl) — exclusão mútua entre PROCESSOS (cron + bot)."""
    lock_path = Path(db_path).parent / ".monitor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Outro scraping já está em execução; tente novamente em instantes."
                    )
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class MonitorService:
    def __init__(self, db_path: Path, source_registry: SourceRegistry | None = None):
        self.db_path = db_path
        self.source_registry = source_registry or SourceRegistry()

    def scrape_and_persist(self, sources: list[str] | None = None,
                           *, mode: str = "snapshot") -> ScrapeRunResult:
        with monitor_file_lock(self.db_path):
            return self._scrape_and_persist_locked(sources, mode=mode)

    def _scrape_and_persist_locked(self, sources: list[str] | None,
                                   *, mode: str) -> ScrapeRunResult:
        scrapers = self.source_registry.all()
        selected = sources or list(scrapers)
        all_products: list[Product] = []
        failed: list[str] = []

        with connect(self.db_path) as conn:
            run_id = start_run(conn, mode=mode, sources=selected)

        for name in selected:
            scraper_cls = scrapers.get(name)
            source_started = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            if not scraper_cls:
                log.error("Fonte desconhecida: %s", name)
                failed.append(name)
                with connect(self.db_path) as conn:
                    record_source_run(
                        conn, run_id=run_id, source=name, started_at=source_started,
                        status="failed", error="Fonte desconhecida",
                    )
                continue

            try:
                log.info(">>> %s: iniciando scraping", name)
                products = scraper_cls().fetch_all()
                kept: list[Product] = []
                drop_g = drop_s = 0
                for p in products:
                    ok, reason = should_keep_product(p)
                    if ok:
                        kept.append(p)
                    elif reason == "gender":
                        drop_g += 1
                    else:
                        drop_s += 1
                if drop_g or drop_s:
                    log.info(
                        ">>> %s: %d produtos (%d brutos; -%d gênero/idade, -%d tamanho 42/43)",
                        name, len(kept), len(products), drop_g, drop_s,
                    )
                else:
                    log.info(">>> %s: %d produtos", name, len(kept))
                all_products.extend(kept)
                with connect(self.db_path) as conn:
                    record_source_run(
                        conn, run_id=run_id, source=name, started_at=source_started,
                        status="success", raw_count=len(products), kept_count=len(kept),
                        drop_gender=drop_g, drop_size=drop_s,
                    )
            except Exception:  # noqa: BLE001
                log.exception(">>> %s: falhou", name)
                failed.append(name)
                with connect(self.db_path) as conn:
                    record_source_run(
                        conn, run_id=run_id, source=name, started_at=source_started,
                        status="failed", error="Falha durante scraping; consulte logs.",
                    )

        status = "success"
        if failed and all_products:
            status = "partial"
        elif failed and not all_products:
            status = "failed"
        counters = RunCounters()
        with connect(self.db_path) as conn:
            if all_products:
                counters = RunCounters.from_mapping(
                    record_run(conn, all_products, run_id=run_id))
            finish_run(conn, run_id, status=status,
                       error=", ".join(failed) if failed else None)
        return ScrapeRunResult(all_products, failed, counters)

    def run(self, *, sources: list[str] | None = None, mode: str = "alert",
            digest_hours: int = 24) -> MonitorResult:
        now = datetime.now(timezone.utc)
        cutoff_dt = now - (timedelta(hours=digest_hours)
                           if mode == "digest" else timedelta(seconds=10))
        cutoff_iso = cutoff_dt.isoformat(timespec="seconds")
        scrape = self.scrape_and_persist(sources, mode=mode)
        changes = {k: [] for k in CHANGE_CATEGORIES}
        if scrape.products:
            with connect(self.db_path) as conn:
                changes = find_changes(conn, cutoff_iso)
        return MonitorResult(scrape, changes, mode, cutoff_iso)

    def snapshot(self, *, sources: list[str] | None = None) -> SnapshotResult:
        scrape = self.scrape_and_persist(sources, mode="snapshot")
        changes = {k: [] for k in CHANGE_CATEGORIES}
        if scrape.products:
            with connect(self.db_path) as conn:
                changes = snapshot_promotions(conn)
        return SnapshotResult(scrape, changes)


class CatalogService:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def latest_discounted(self, *, source: str | None = None,
                          filters: ProductFilters | None = None,
                          limit: int | None = None) -> list[sqlite3.Row]:
        filters = filters or ProductFilters()
        query = """
            SELECT p.source, p.sku, p.name, p.url, p.image,
                   ph.price, ph.list_price, ph.sizes, ph.stock_qty,
                   ROUND((1 - ph.price / ph.list_price) * 100) as discount_pct
              FROM products p
              JOIN price_history ph ON p.source = ph.source AND p.sku = ph.sku
              JOIN (
                    SELECT source, sku, MAX(observed_at) AS latest
                      FROM price_history
                     GROUP BY source, sku
              ) latest_ph
                ON ph.source = latest_ph.source
               AND ph.sku = latest_ph.sku
               AND ph.observed_at = latest_ph.latest
             WHERE ph.list_price IS NOT NULL
               AND ph.list_price > 0
               AND ph.price < ph.list_price
        """
        params: list = []
        if source:
            query += " AND p.source = ?"
            params.append(source)
        query += _category_sql(filters.category)
        if filters.max_price != "all":
            query += " AND ph.price <= ?"
            params.append(float(filters.max_price))
        if filters.min_discount != "all":
            query += " AND ((1 - ph.price / ph.list_price) * 100) >= ?"
            params.append(float(filters.min_discount))
        query += " ORDER BY ph.price / ph.list_price ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with connect(self.db_path) as conn:
            return list(conn.execute(query, params))

    def store_status(self) -> list[sqlite3.Row]:
        with connect(self.db_path) as conn:
            return list(conn.execute("""
                SELECT p.source, COUNT(DISTINCT p.sku) as products,
                       MIN(ph.observed_at) as oldest, MAX(ph.observed_at) as newest
                  FROM products p
                  JOIN price_history ph ON p.source = ph.source AND p.sku = ph.sku
                 GROUP BY p.source
                 ORDER BY p.source
            """))

    def db_stats(self) -> dict[str, int | str]:
        with connect(self.db_path) as conn:
            total_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            total_observations = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
            active_discounts = conn.execute("""
                SELECT COUNT(DISTINCT p.source || ':' || p.sku)
                  FROM products p
                  JOIN price_history ph ON p.source = ph.source AND p.sku = ph.sku
                  JOIN (
                        SELECT source, sku, MAX(observed_at) AS latest
                          FROM price_history GROUP BY source, sku
                  ) latest_ph
                    ON ph.source = latest_ph.source
                   AND ph.sku = latest_ph.sku
                   AND ph.observed_at = latest_ph.latest
                 WHERE ph.price < ph.list_price AND ph.list_price > 0
            """).fetchone()[0]
        db_size = os.path.getsize(self.db_path) if self.db_path.exists() else 0
        return {
            "total_products": total_products,
            "total_observations": total_observations,
            "active_discounts": active_discounts,
            "db_size": db_size,
        }

    def purge_candidates(self) -> PurgeResult:
        with connect(self.db_path) as conn:
            rows = list(conn.execute("""
                SELECT p.source, p.sku, p.name, h.sizes
                  FROM products p
                  JOIN price_history h
                    ON h.source = p.source AND h.sku = p.sku
                   AND h.observed_at = (
                       SELECT MAX(observed_at) FROM price_history
                        WHERE source = p.source AND sku = p.sku
                   )
            """))
            candidates: list[PurgeCandidate] = []
            observations = 0
            for r in rows:
                sizes = (r["sizes"] or "").split(",") if r["sizes"] else ()
                keep, reason = should_keep(r["name"] or "", sizes)
                if not keep:
                    candidates.append(PurgeCandidate(r["source"], r["sku"], r["name"] or "", reason))
                    observations += conn.execute(
                        "SELECT COUNT(*) FROM price_history WHERE source=? AND sku=?",
                        (r["source"], r["sku"]),
                    ).fetchone()[0]
            return PurgeResult(candidates, observations, applied=False)

    def purge_apply(self) -> PurgeResult:
        result = self.purge_candidates()
        if not result.candidates:
            return result
        with connect(self.db_path) as conn:
            for c in result.candidates:
                conn.execute("DELETE FROM price_history WHERE source=? AND sku=?", (c.source, c.sku))
                conn.execute("DELETE FROM products WHERE source=? AND sku=?", (c.source, c.sku))
        return PurgeResult(result.candidates, result.observations, applied=True)

    def normalize_dry(self) -> NormalizeResult:
        old_threshold = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        stale_threshold = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        with connect(self.db_path) as conn:
            old_observations = conn.execute(
                "SELECT COUNT(*) FROM price_history WHERE observed_at < ?",
                (old_threshold,),
            ).fetchone()[0]
            stale_products = conn.execute(
                "SELECT COUNT(*) FROM products WHERE last_seen < ?",
                (stale_threshold,),
            ).fetchone()[0]
            bad_price = conn.execute(_bad_price_query("COUNT(*)")).fetchone()[0]
        return NormalizeResult(old_observations, stale_products, bad_price)

    def normalize_apply(self) -> NormalizeResult:
        dry = self.normalize_dry()
        old_threshold = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        removed = 0
        with connect(self.db_path) as conn:
            result = conn.execute("DELETE FROM price_history WHERE observed_at < ?", (old_threshold,))
            removed += result.rowcount
            rows = list(conn.execute(_bad_price_query("ph.source, ph.sku")))
            for row in rows:
                conn.execute("DELETE FROM price_history WHERE source=? AND sku=?", (row["source"], row["sku"]))
                conn.execute("DELETE FROM products WHERE source=? AND sku=?", (row["source"], row["sku"]))
                removed += 1
        return NormalizeResult(dry.old_observations, dry.stale_products,
                               dry.bad_price_products, removed, applied=True)


_scrape_lock = Lock()


def run_exclusive(fn):
    if not _scrape_lock.acquire(blocking=False):
        raise RuntimeError("Já existe uma varredura em andamento. Tente novamente em alguns minutos.")
    try:
        return fn()
    finally:
        _scrape_lock.release()


def _category_sql(category: str) -> str:
    if category == "all":
        return ""
    categories = {
        "tenis": " AND (LOWER(p.name) LIKE '%tênis%' OR LOWER(p.name) LIKE '%tenis%' OR LOWER(p.name) LIKE '%chinelo%' OR LOWER(p.name) LIKE '%chuteira%')",
        "vestuario": " AND (LOWER(p.name) LIKE '%camiseta%' OR LOWER(p.name) LIKE '%camisa%' OR LOWER(p.name) LIKE '%moletom%' OR LOWER(p.name) LIKE '%jaqueta%' OR LOWER(p.name) LIKE '%calça%' OR LOWER(p.name) LIKE '%calca%' OR LOWER(p.name) LIKE '%bermuda%' OR LOWER(p.name) LIKE '%short%' OR LOWER(p.name) LIKE '%meia%')",
        "acessorios": " AND (LOWER(p.name) LIKE '%boné%' OR LOWER(p.name) LIKE '%bone%' OR LOWER(p.name) LIKE '%gorro%' OR LOWER(p.name) LIKE '%mochila%' OR LOWER(p.name) LIKE '%shoulder%' OR LOWER(p.name) LIKE '%bag%' OR LOWER(p.name) LIKE '%cinto%' OR LOWER(p.name) LIKE '%óculos%' OR LOWER(p.name) LIKE '%oculos%')",
        "camisas_time": " AND (LOWER(p.name) LIKE '%camisa%' AND (LOWER(p.name) LIKE '%time%' OR LOWER(p.name) LIKE '%torcida%' OR LOWER(p.name) LIKE '%seleção%' OR LOWER(p.name) LIKE '%selecao%' OR LOWER(p.name) LIKE '%clube%' OR LOWER(p.name) LIKE '%fan%'))",
        "agasalhos": " AND (LOWER(p.name) LIKE '%agasalho%' OR LOWER(p.name) LIKE '%moletom%' OR LOWER(p.name) LIKE '%corta vento%' OR LOWER(p.name) LIKE '%jaqueta%' OR LOWER(p.name) LIKE '%windbreaker%' OR LOWER(p.name) LIKE '%blusa%' OR LOWER(p.name) LIKE '%suéter%' OR LOWER(p.name) LIKE '%sweter%')",
    }
    return categories.get(category, "")


def _bad_price_query(select_expr: str) -> str:
    return f"""
        SELECT {select_expr}
          FROM products p
          JOIN price_history ph ON p.source = ph.source AND p.sku = ph.sku
          JOIN (
                SELECT source, sku, MAX(observed_at) AS latest
                  FROM price_history GROUP BY source, sku
          ) latest_ph
            ON ph.source = latest_ph.source
           AND ph.sku = latest_ph.sku
           AND ph.observed_at = latest_ph.latest
         WHERE ph.price <= 0 OR ph.price IS NULL
    """


def html_error(prefix: str, exc: Exception) -> str:
    return f"❌ <b>{escape(prefix)}:</b>\n<pre>{escape(str(exc))}</pre>"
