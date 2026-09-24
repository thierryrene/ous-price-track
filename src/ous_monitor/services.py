from __future__ import annotations

import fcntl
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from threading import Lock
from typing import Callable

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
class ScrapeProgress:
    source: str
    event: str
    completed: int
    total: int
    success: bool | None = None
    raw_count: int = 0
    kept_count: int = 0
    error: str | None = None


@dataclass(frozen=True)
class _SourceOutcome:
    index: int
    source: str
    started_at: str
    products: list[Product]
    raw_count: int = 0
    drop_gender: int = 0
    drop_size: int = 0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


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
class MaintenanceResult:
    backup_path: Path
    retention_days: int
    removed_observations: int
    removed_bad_products: int
    removed_runs: int
    before_bytes: int
    after_bytes: int
    max_bytes: int
    vacuumed: bool

    @property
    def within_size_limit(self) -> bool:
        return self.after_bytes <= self.max_bytes


@dataclass(frozen=True)
class SourceFreshness:
    source: str
    run_id: str | None
    status: str
    checked_at: str | None
    error: str | None
    successful_run_id: str | None
    last_success_at: str | None
    age_seconds: float | None

    @property
    def has_snapshot(self) -> bool:
        return self.successful_run_id is not None


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
    def __init__(self, db_path: Path, source_registry: SourceRegistry | None = None,
                 *, max_workers: int | None = None):
        self.db_path = db_path
        self.source_registry = source_registry or SourceRegistry()
        configured_workers = max_workers
        if configured_workers is None:
            try:
                configured_workers = int(os.getenv("SCRAPE_MAX_WORKERS", "4"))
            except ValueError:
                configured_workers = 4
        self.max_workers = max(1, configured_workers)

    def scrape_and_persist(self, sources: list[str] | None = None,
                           *, mode: str = "snapshot",
                           progress: Callable[[ScrapeProgress], None] | None = None,
                           ) -> ScrapeRunResult:
        with monitor_file_lock(self.db_path):
            return self._scrape_and_persist_locked(
                sources, mode=mode, progress=progress,
            )

    def _scrape_and_persist_locked(self, sources: list[str] | None,
                                   *, mode: str,
                                   progress: Callable[[ScrapeProgress], None] | None = None,
                                   ) -> ScrapeRunResult:
        scrapers = self.source_registry.all()
        selected = sources or list(scrapers)

        with connect(self.db_path) as conn:
            run_id = start_run(conn, mode=mode, sources=selected)

        total = len(selected)
        progress_lock = Lock()
        completed = 0

        def emit(event: ScrapeProgress) -> None:
            if progress is None:
                return
            try:
                progress(event)
            except Exception:  # noqa: BLE001
                log.exception("Callback de progresso falhou para %s", event.source)

        def scrape_source(index: int, name: str) -> _SourceOutcome:
            nonlocal completed
            source_started = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            with progress_lock:
                emit(ScrapeProgress(name, "start", completed, total))
            scraper_cls = scrapers.get(name)
            if not scraper_cls:
                log.error("Fonte desconhecida: %s", name)
                outcome = _SourceOutcome(
                    index, name, source_started, [], error="Fonte desconhecida",
                )
            else:
                try:
                    log.info(">>> %s: iniciando scraping", name)
                    products = scraper_cls().fetch_all()
                    kept: list[Product] = []
                    drop_g = drop_s = 0
                    for product in products:
                        ok, reason = should_keep_product(product)
                        if ok:
                            kept.append(product)
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
                    outcome = _SourceOutcome(
                        index, name, source_started, kept,
                        raw_count=len(products), drop_gender=drop_g, drop_size=drop_s,
                    )
                except Exception:  # noqa: BLE001
                    log.exception(">>> %s: falhou", name)
                    outcome = _SourceOutcome(
                        index, name, source_started, [],
                        error="Falha durante scraping; consulte logs.",
                    )
            with progress_lock:
                completed += 1
                emit(ScrapeProgress(
                    name, "finish", completed, total, success=outcome.success,
                    raw_count=outcome.raw_count, kept_count=len(outcome.products),
                    error=outcome.error,
                ))
            return outcome

        def scrape_group(group: list[tuple[int, str]]) -> list[_SourceOutcome]:
            return [scrape_source(index, name) for index, name in group]

        netshoes_group = [
            (index, name) for index, name in enumerate(selected)
            if name == "netshoes" or name.startswith("netshoes_")
        ]
        independent_groups = [
            [(index, name)] for index, name in enumerate(selected)
            if name != "netshoes" and not name.startswith("netshoes_")
        ]
        # O grupo mais longo entra primeiro no pool; internamente as fontes do
        # mesmo host continuam estritamente sequenciais para evitar mais 429.
        groups = ([netshoes_group] if netshoes_group else []) + independent_groups

        outcomes: dict[int, _SourceOutcome] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(scrape_group, group) for group in groups]
            for future in as_completed(futures):
                group_outcomes = future.result()
                with connect(self.db_path) as conn:
                    for outcome in group_outcomes:
                        outcomes[outcome.index] = outcome
                        record_source_run(
                            conn, run_id=run_id, source=outcome.source,
                            started_at=outcome.started_at,
                            status="success" if outcome.success else "failed",
                            raw_count=outcome.raw_count,
                            kept_count=len(outcome.products),
                            drop_gender=outcome.drop_gender,
                            drop_size=outcome.drop_size,
                            error=outcome.error,
                        )

        ordered_outcomes = [outcomes[index] for index in range(total)]
        all_products = [
            product for outcome in ordered_outcomes for product in outcome.products
        ]
        failed = [outcome.source for outcome in ordered_outcomes if not outcome.success]

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
            digest_hours: int = 24,
            progress: Callable[[ScrapeProgress], None] | None = None,
            ) -> MonitorResult:
        now = datetime.now(timezone.utc)
        cutoff_dt = now - (timedelta(hours=digest_hours)
                           if mode == "digest" else timedelta(seconds=10))
        cutoff_iso = cutoff_dt.isoformat(timespec="seconds")
        scrape = self.scrape_and_persist(sources, mode=mode, progress=progress)
        changes = {k: [] for k in CHANGE_CATEGORIES}
        if scrape.products:
            with connect(self.db_path) as conn:
                changes = find_changes(conn, cutoff_iso)
        return MonitorResult(scrape, changes, mode, cutoff_iso)

    def snapshot(self, *, sources: list[str] | None = None,
                 progress: Callable[[ScrapeProgress], None] | None = None,
                 ) -> SnapshotResult:
        scrape = self.scrape_and_persist(
            sources, mode="snapshot", progress=progress,
        )
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
        query = _latest_discounted_sql("""
            p.source, p.sku, p.name, p.url, p.image,
            ph.price, ph.list_price, ph.sizes, ph.stock_qty,
            ROUND((1 - ph.price / ph.list_price) * 100) as discount_pct
        """)
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

    def filter_option_counts(
        self, source: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Count each filter bucket in the latest successful source snapshot.

        Counts are independent: each option describes how many currently
        available promotions it matches before other filters are applied.
        """
        category_keys = (
            "tenis", "vestuario", "acessorios", "camisas_time", "agasalhos",
        )
        aggregates = ["COUNT(*) AS category_all"]
        aggregates.extend(
            f"COALESCE(SUM(CASE WHEN {_category_condition(key)} "
            f"THEN 1 ELSE 0 END), 0) AS category_{key}"
            for key in category_keys
        )
        aggregates.extend((
            "COALESCE(SUM(CASE WHEN ph.price <= 100 THEN 1 ELSE 0 END), 0) AS price_100",
            "COALESCE(SUM(CASE WHEN ph.price <= 200 THEN 1 ELSE 0 END), 0) AS price_200",
            "COALESCE(SUM(CASE WHEN ph.price <= 500 THEN 1 ELSE 0 END), 0) AS price_500",
            "COUNT(*) AS price_all",
            "COALESCE(SUM(CASE WHEN ((1 - ph.price / ph.list_price) * 100) >= 50 THEN 1 ELSE 0 END), 0) AS discount_50",
            "COALESCE(SUM(CASE WHEN ((1 - ph.price / ph.list_price) * 100) >= 30 THEN 1 ELSE 0 END), 0) AS discount_30",
            "COUNT(*) AS discount_all",
        ))
        query = _latest_discounted_sql(",\n".join(aggregates))
        params: list = []
        if source:
            query += " AND p.source = ?"
            params.append(source)

        with connect(self.db_path) as conn:
            row = conn.execute(query, params).fetchone()

        return {
            "category": {
                **{key: int(row[f"category_{key}"]) for key in category_keys},
                "all": int(row["category_all"]),
            },
            "max_price": {
                key: int(row[f"price_{key}"])
                for key in ("100", "200", "500", "all")
            },
            "min_discount": {
                key: int(row[f"discount_{key}"])
                for key in ("50", "30", "all")
            },
        }

    def freshness(self, *, source: str | None = None) -> list[SourceFreshness]:
        """Última tentativa e último snapshot completo por fonte."""
        query = """
            WITH attempts AS (
                SELECT sr.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY sr.source
                           ORDER BY sr.started_at DESC
                       ) AS rn
                  FROM source_runs sr
                  JOIN runs r ON r.id = sr.run_id
                 WHERE r.finished_at IS NOT NULL
            ),
            successes AS (
                SELECT sr.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY sr.source
                           ORDER BY sr.finished_at DESC, sr.started_at DESC
                       ) AS rn
                  FROM source_runs sr
                  JOIN runs r ON r.id = sr.run_id
                 WHERE sr.status = 'success'
                   AND sr.finished_at IS NOT NULL
                   AND r.finished_at IS NOT NULL
            ),
            sources AS (
                SELECT source FROM attempts WHERE rn = 1
                UNION
                SELECT source FROM successes WHERE rn = 1
            )
            SELECT src.source,
                   a.run_id, COALESCE(a.status, 'unknown') AS status,
                   a.finished_at AS checked_at, a.error,
                   s.run_id AS successful_run_id,
                   s.finished_at AS last_success_at
              FROM sources src
              LEFT JOIN attempts a ON a.source = src.source AND a.rn = 1
              LEFT JOIN successes s ON s.source = src.source AND s.rn = 1
             WHERE (? IS NULL OR src.source = ?)
             ORDER BY src.source
        """
        now = datetime.now(timezone.utc)
        with connect(self.db_path) as conn:
            rows = list(conn.execute(query, (source, source)))
        result = []
        for row in rows:
            last_success_at = row["last_success_at"]
            age_seconds = None
            if last_success_at:
                verified_dt = datetime.fromisoformat(last_success_at)
                if verified_dt.tzinfo is None:
                    verified_dt = verified_dt.replace(tzinfo=timezone.utc)
                age_seconds = max(0.0, (now - verified_dt).total_seconds())
            result.append(SourceFreshness(
                source=row["source"], run_id=row["run_id"], status=row["status"],
                checked_at=row["checked_at"], error=row["error"],
                successful_run_id=row["successful_run_id"],
                last_success_at=last_success_at, age_seconds=age_seconds,
            ))
        return result

    def source_freshness(self, source: str) -> SourceFreshness | None:
        rows = self.freshness(source=source)
        return rows[0] if rows else None

    def store_status(self) -> list[sqlite3.Row]:
        with connect(self.db_path) as conn:
            return list(conn.execute("""
                WITH successful_runs AS (
                    SELECT sr.source, sr.run_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY sr.source
                               ORDER BY sr.finished_at DESC, sr.started_at DESC
                           ) AS rn
                      FROM source_runs sr
                      JOIN runs r ON r.id = sr.run_id
                     WHERE sr.status = 'success'
                       AND sr.finished_at IS NOT NULL
                       AND r.finished_at IS NOT NULL
                       AND r.status IN ('success', 'partial')
                )
                SELECT p.source, COUNT(DISTINCT p.sku) as products,
                       MIN(p.first_seen) as oldest, MAX(p.last_seen) as newest
                  FROM products p
                  JOIN successful_runs sr
                    ON sr.source = p.source
                   AND sr.rn = 1
                   AND sr.run_id = p.last_seen_run_id
                 GROUP BY p.source
                 ORDER BY p.source
            """))

    def db_stats(self) -> dict[str, int | str]:
        with connect(self.db_path) as conn:
            total_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            total_observations = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
            active_discounts = conn.execute("""
                WITH successful_runs AS (
                    SELECT sr.source, sr.run_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY sr.source
                               ORDER BY sr.finished_at DESC, sr.started_at DESC
                           ) AS rn
                      FROM source_runs sr
                      JOIN runs r ON r.id = sr.run_id
                     WHERE sr.status = 'success'
                       AND sr.finished_at IS NOT NULL
                       AND r.finished_at IS NOT NULL
                       AND r.status IN ('success', 'partial')
                ),
                latest_ph AS (
                    SELECT source, sku, MAX(observed_at) AS latest
                      FROM price_history
                     GROUP BY source, sku
                )
                SELECT COUNT(*)
                  FROM products p
                  JOIN successful_runs sr
                    ON sr.source = p.source
                   AND sr.rn = 1
                   AND sr.run_id = p.last_seen_run_id
                  JOIN price_history ph ON p.source = ph.source AND p.sku = ph.sku
                  JOIN latest_ph
                    ON ph.source = latest_ph.source
                   AND ph.sku = latest_ph.sku
                   AND ph.observed_at = latest_ph.latest
                 WHERE ph.available = 1
                   AND ph.price < ph.list_price
                   AND ph.list_price > 0
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

    def normalize_dry(self, *, retention_days: int = 90) -> NormalizeResult:
        old_threshold = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        stale_threshold = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        with connect(self.db_path) as conn:
            old_observations = conn.execute(
                _old_observations_query("COUNT(*)"),
                (old_threshold,),
            ).fetchone()[0]
            stale_products = conn.execute(
                "SELECT COUNT(*) FROM products WHERE last_seen < ?",
                (stale_threshold,),
            ).fetchone()[0]
            bad_price = conn.execute(_bad_price_query("COUNT(*)")).fetchone()[0]
        return NormalizeResult(old_observations, stale_products, bad_price)

    def normalize_apply(self, *, retention_days: int = 90) -> NormalizeResult:
        with monitor_file_lock(self.db_path):
            return self._normalize_apply_locked(retention_days=retention_days)

    def _normalize_apply_locked(self, *, retention_days: int) -> NormalizeResult:
        dry = self.normalize_dry(retention_days=retention_days)
        old_threshold = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        removed = 0
        with connect(self.db_path) as conn:
            result = conn.execute(
                _old_observations_query("DELETE"),
                (old_threshold,),
            )
            removed += result.rowcount
            rows = list(conn.execute(_bad_price_query("ph.source, ph.sku")))
            for row in rows:
                conn.execute("DELETE FROM price_history WHERE source=? AND sku=?", (row["source"], row["sku"]))
                conn.execute("DELETE FROM products WHERE source=? AND sku=?", (row["source"], row["sku"]))
                removed += 1
        return NormalizeResult(dry.old_observations, dry.stale_products,
                               dry.bad_price_products, removed, applied=True)

    def maintain(
        self,
        *,
        retention_days: int = 90,
        max_db_mb: int = 50,
        backup_keep: int = 7,
        backup_dir: Path | None = None,
        run_retention_days: int = 180,
    ) -> MaintenanceResult:
        """Back up and compact the DB while preserving each SKU's latest state."""
        if retention_days < 1 or run_retention_days < 1:
            raise ValueError("retention_days deve ser positivo")
        if max_db_mb < 1 or backup_keep < 1:
            raise ValueError("max_db_mb e backup_keep devem ser positivos")

        backup_dir = backup_dir or self.db_path.parent / "backups"
        max_bytes = max_db_mb * 1024 * 1024

        with monitor_file_lock(self.db_path, timeout_s=30.0):
            # Inicializa/migra o schema antes do backup, inclusive em uma base nova.
            with connect(self.db_path) as conn:
                conn.execute("SELECT 1")
            before_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
            backup_path = self._backup_locked(backup_dir, backup_keep)
            normalized = self._normalize_apply_locked(retention_days=retention_days)

            runs_threshold = (
                datetime.now(timezone.utc) - timedelta(days=run_retention_days)
            ).isoformat()
            with connect(self.db_path) as conn:
                removed_runs = conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE started_at < ?",
                    (runs_threshold,),
                ).fetchone()[0]
                conn.execute(
                    """
                    DELETE FROM source_runs
                     WHERE run_id IN (
                         SELECT id FROM runs WHERE started_at < ?
                     )
                    """,
                    (runs_threshold,),
                )
                conn.execute(
                    "DELETE FROM runs WHERE started_at < ?",
                    (runs_threshold,),
                )

            vacuumed = bool(normalized.removed or removed_runs or before_bytes > max_bytes)
            if vacuumed:
                conn = sqlite3.connect(self.db_path)
                try:
                    conn.execute("PRAGMA busy_timeout = 30000")
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
                finally:
                    conn.close()

            after_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0

        return MaintenanceResult(
            backup_path=backup_path,
            retention_days=retention_days,
            removed_observations=normalized.old_observations,
            removed_bad_products=normalized.bad_price_products,
            removed_runs=removed_runs,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            max_bytes=max_bytes,
            vacuumed=vacuumed,
        )

    def _backup_locked(self, backup_dir: Path, backup_keep: int) -> Path:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_path = backup_dir / f"prices-{stamp}.db"

        source = sqlite3.connect(self.db_path)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

        backups = sorted(backup_dir.glob("prices-*.db"), reverse=True)
        for old_backup in backups[backup_keep:]:
            old_backup.unlink()
        return backup_path


_scrape_lock = Lock()


def run_exclusive(fn):
    if not _scrape_lock.acquire(blocking=False):
        raise RuntimeError("Já existe uma varredura em andamento. Tente novamente em alguns minutos.")
    try:
        return fn()
    finally:
        _scrape_lock.release()


_CATEGORY_CONDITIONS = {
    "tenis": "(LOWER(p.name) LIKE '%tênis%' OR LOWER(p.name) LIKE '%tenis%' OR LOWER(p.name) LIKE '%chinelo%' OR LOWER(p.name) LIKE '%chuteira%')",
    "vestuario": "(LOWER(p.name) LIKE '%camiseta%' OR LOWER(p.name) LIKE '%camisa%' OR LOWER(p.name) LIKE '%moletom%' OR LOWER(p.name) LIKE '%jaqueta%' OR LOWER(p.name) LIKE '%calça%' OR LOWER(p.name) LIKE '%calca%' OR LOWER(p.name) LIKE '%bermuda%' OR LOWER(p.name) LIKE '%short%' OR LOWER(p.name) LIKE '%meia%')",
    "acessorios": "(LOWER(p.name) LIKE '%boné%' OR LOWER(p.name) LIKE '%bone%' OR LOWER(p.name) LIKE '%gorro%' OR LOWER(p.name) LIKE '%mochila%' OR LOWER(p.name) LIKE '%shoulder%' OR LOWER(p.name) LIKE '%bag%' OR LOWER(p.name) LIKE '%cinto%' OR LOWER(p.name) LIKE '%óculos%' OR LOWER(p.name) LIKE '%oculos%')",
    "camisas_time": "(LOWER(p.name) LIKE '%camisa%' AND (LOWER(p.name) LIKE '%time%' OR LOWER(p.name) LIKE '%torcida%' OR LOWER(p.name) LIKE '%seleção%' OR LOWER(p.name) LIKE '%selecao%' OR LOWER(p.name) LIKE '%clube%' OR LOWER(p.name) LIKE '%fan%'))",
    "agasalhos": "(LOWER(p.name) LIKE '%agasalho%' OR LOWER(p.name) LIKE '%moletom%' OR LOWER(p.name) LIKE '%corta vento%' OR LOWER(p.name) LIKE '%jaqueta%' OR LOWER(p.name) LIKE '%windbreaker%' OR LOWER(p.name) LIKE '%blusa%' OR LOWER(p.name) LIKE '%suéter%' OR LOWER(p.name) LIKE '%sweter%')",
}


def _category_condition(category: str) -> str:
    return _CATEGORY_CONDITIONS.get(category, "1")


def _category_sql(category: str) -> str:
    if category == "all" or category not in _CATEGORY_CONDITIONS:
        return ""
    return f" AND {_category_condition(category)}"


def _latest_discounted_sql(select_clause: str) -> str:
    """Shared SQL base for the current, available promotion catalog."""
    return f"""
        WITH successful_runs AS (
            SELECT sr.source, sr.run_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY sr.source
                       ORDER BY sr.finished_at DESC, sr.started_at DESC
                   ) AS rn
              FROM source_runs sr
              JOIN runs r ON r.id = sr.run_id
             WHERE sr.status = 'success'
               AND sr.finished_at IS NOT NULL
               AND r.finished_at IS NOT NULL
        ),
        latest_ph AS (
            SELECT source, sku, MAX(observed_at) AS latest
              FROM price_history
             GROUP BY source, sku
        )
        SELECT {select_clause}
          FROM products p
          JOIN successful_runs sr
            ON sr.source = p.source
           AND sr.rn = 1
           AND sr.run_id = p.last_seen_run_id
          JOIN price_history ph ON p.source = ph.source AND p.sku = ph.sku
          JOIN latest_ph
            ON ph.source = latest_ph.source
           AND ph.sku = latest_ph.sku
           AND ph.observed_at = latest_ph.latest
         WHERE ph.list_price IS NOT NULL
           AND ph.list_price > 0
           AND ph.price < ph.list_price
           AND ph.available = 1
    """


def _old_observations_query(select_expr: str) -> str:
    """Select/delete expired history while always retaining each SKU's latest row."""
    where = """
         WHERE price_history.observed_at < ?
           AND EXISTS (
               SELECT 1
                 FROM price_history newer
                WHERE newer.source = price_history.source
                  AND newer.sku = price_history.sku
                  AND newer.observed_at > price_history.observed_at
           )
    """
    if select_expr == "DELETE":
        return "DELETE FROM price_history" + where
    return f"SELECT {select_expr} FROM price_history" + where


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
