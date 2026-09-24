from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ous_monitor.models import Product
from ous_monitor.services import CatalogService
from ous_monitor.storage import (
    connect,
    finish_run,
    record_run,
    record_source_run,
    start_run,
)


def _product(
    sku: str,
    name: str,
    price: float,
    list_price: float | None,
    *,
    source: str = "test",
    available: bool = True,
) -> Product:
    return Product(
        source=source,
        sku=sku,
        name=name,
        url=f"https://example.test/{sku}",
        image=None,
        list_price=list_price,
        price=price,
        available=available,
        sizes=["42"],
    )


def _record_successful_snapshot(
    conn, source: str, products: list[Product], *, at: str,
) -> str:
    with patch("ous_monitor.storage._now", return_value=at):
        run_id = start_run(conn, mode="snapshot", sources=[source])
        record_source_run(
            conn,
            run_id=run_id,
            source=source,
            started_at=at,
            status="success",
            raw_count=len(products),
            kept_count=len(products),
        )
        record_run(conn, products, run_id=run_id)
        finish_run(conn, run_id, status="success")
    return run_id


class FilterOptionCountsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "prices.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_counts_filter_buckets_from_latest_successful_snapshot(self) -> None:
        with connect(self.db) as conn:
            _record_successful_snapshot(
                conn,
                "test",
                [_product("stale", "Tênis antigo", 20, 100)],
                at="2026-01-01T00:00:00+00:00",
            )
            _record_successful_snapshot(
                conn,
                "test",
                [
                    _product("tenis", "Tênis Runner", 90, 200),
                    _product("time", "Camisa de time", 150, 300),
                    _product("moletom", "Moletom clássico", 350, 500),
                    _product("bone", "Boné casual", 450, 500),
                    _product("full", "Chinelo sem desconto", 80, 80),
                    _product(
                        "unavailable", "Jaqueta indisponível", 50, 200,
                        available=False,
                    ),
                ],
                at="2026-01-01T01:00:00+00:00",
            )
            _record_successful_snapshot(
                conn,
                "other",
                [_product(
                    "other-tenis", "Tênis outra fonte", 10, 100,
                    source="other",
                )],
                at="2026-01-01T02:00:00+00:00",
            )

        counts = CatalogService(self.db).filter_option_counts("test")

        self.assertEqual(counts["category"], {
            "tenis": 1,
            "vestuario": 2,
            "acessorios": 1,
            "camisas_time": 1,
            "agasalhos": 1,
            "all": 4,
        })
        self.assertEqual(counts["max_price"], {
            "100": 1,
            "200": 2,
            "500": 4,
            "all": 4,
        })
        self.assertEqual(counts["min_discount"], {
            "50": 2,
            "30": 3,
            "all": 4,
        })

    def test_returns_zeroes_without_successful_snapshot(self) -> None:
        counts = CatalogService(self.db).filter_option_counts("missing")

        self.assertEqual(counts, {
            "category": {
                "tenis": 0,
                "vestuario": 0,
                "acessorios": 0,
                "camisas_time": 0,
                "agasalhos": 0,
                "all": 0,
            },
            "max_price": {"100": 0, "200": 0, "500": 0, "all": 0},
            "min_discount": {"50": 0, "30": 0, "all": 0},
        })


if __name__ == "__main__":
    unittest.main()
