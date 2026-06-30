"""Scraper para o outlet oficial da Umbro.

umbro.com.br roda em VTEX IO. A página `/outlet` é a coleção `921` ("Outlet")
e a API pública `catalog_system` pagina a coleção com `_from/_to`, retornando o
total real no header `resources`.
"""
from __future__ import annotations

import logging
import time
from typing import Iterator, Optional

import httpx

from ..models import Product

log = logging.getLogger(__name__)

BASE_URL = "https://www.umbro.com.br/api/catalog_system/pub/products/search"
OUTLET_CLUSTER_ID = "921"
PAGE_SIZE = 50
REQUEST_DELAY_S = 0.7
TIMEOUT_S = 30.0
HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}


def _parse_total(resources_header: str | None) -> int | None:
    if not resources_header or "/" not in resources_header:
        return None
    try:
        return int(resources_header.rsplit("/", 1)[1])
    except ValueError:
        return None


def _iter_pages(client: httpx.Client) -> Iterator[list[dict]]:
    start = 0
    total: int | None = None
    while True:
        end = start + PAGE_SIZE - 1
        resp = client.get(
            BASE_URL,
            params={
                "fq": f"H:{OUTLET_CLUSTER_ID}",
                "_from": start,
                "_to": end,
            },
        )
        # VTEX retorna 206 Partial Content para listagens paginadas.
        if resp.status_code not in (200, 206):
            resp.raise_for_status()
        page = resp.json()
        if total is None:
            total = _parse_total(resp.headers.get("resources"))
            log.info("Umbro outlet: total declarado pelo servidor = %s", total)
        if not page:
            return
        yield page
        start += PAGE_SIZE
        if total is not None and start >= total:
            return
        time.sleep(REQUEST_DELAY_S)


def _item_size(item: dict) -> Optional[str]:
    for var_name in item.get("variations") or []:
        if not isinstance(var_name, str):
            continue
        vals = item.get(var_name)
        if isinstance(vals, list) and vals:
            label = str(vals[0]).strip()
            if label:
                return label
    name = item.get("name")
    return str(name).strip() if name else None


def _offer_for_item(item: dict) -> Optional[dict]:
    sellers = item.get("sellers") or []
    if not sellers:
        return None
    return sellers[0].get("commertialOffer") or None


def _to_product(raw: dict) -> Product | None:
    items = raw.get("items") or []
    priced_offers: list[dict] = []
    available_offers: list[dict] = []
    sizes: list[str] = []
    total_qty = 0
    any_qty_reported = False
    image: Optional[str] = None

    for item in items:
        if image is None:
            images = item.get("images") or []
            if images:
                image = images[0].get("imageUrl")

        offer = _offer_for_item(item)
        if not offer or offer.get("Price") is None:
            continue
        priced_offers.append(offer)

        if not offer.get("IsAvailable"):
            continue
        available_offers.append(offer)

        size = _item_size(item)
        if size:
            sizes.append(size)

        qty = offer.get("AvailableQuantity")
        if qty is not None:
            any_qty_reported = True
            total_qty += int(qty)

    if not priced_offers:
        return None

    offer = min(available_offers or priced_offers, key=lambda o: float(o.get("Price") or 0))
    price = offer.get("Price")
    list_price = offer.get("ListPrice")

    return Product(
        source="umbro",
        sku=str(raw["productId"]),
        name=raw.get("productName") or raw.get("productTitle") or "",
        url=raw.get("link", ""),
        image=image,
        list_price=float(list_price) if list_price else None,
        price=float(price),
        available=bool(available_offers),
        brand=raw.get("brand") or "Umbro",
        sizes=sizes,
        stock_qty=total_qty if any_qty_reported else None,
    )


class UmbroScraper:
    source = "umbro"

    def fetch_all(self) -> list[Product]:
        out: list[Product] = []
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT_S, follow_redirects=True) as client:
            for page in _iter_pages(client):
                for raw in page:
                    p = _to_product(raw)
                    if p is not None:
                        out.append(p)
        promos = sum(1 for p in out if p.has_discount)
        log.info("Umbro outlet: %d produtos carregados (%d em promoção)", len(out), promos)
        return out
