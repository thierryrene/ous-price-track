"""Scraper para a categoria Garimpo (outlet) do site oficial OUS.

ous.com.br é VTEX. A categoria Garimpo é a categoryId 6 e contém ~144 produtos
no momento da redação. A API pública do catalog_system aceita até 50 itens por
chamada via os parâmetros _from/_to. O header `resources: X-Y/TOTAL` indica o
total real, então paginamos até esgotar.
"""
from __future__ import annotations

import logging
import time
from typing import Iterator, Optional

import httpx

from ..models import Product

log = logging.getLogger(__name__)

BASE_URL = "https://www.ous.com.br/api/catalog_system/pub/products/search/garimpo"
PAGE_SIZE = 50
REQUEST_DELAY_S = 0.5
TIMEOUT_S = 20.0
HEADERS = {
    "Accept": "application/json",
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
        resp = client.get(BASE_URL, params={"_from": start, "_to": end})
        # VTEX retorna 206 Partial Content para listagens — é sucesso.
        if resp.status_code not in (200, 206):
            resp.raise_for_status()
        page = resp.json()
        if total is None:
            total = _parse_total(resp.headers.get("resources"))
            log.info("OUS garimpo: total declarado pelo servidor = %s", total)
        if not page:
            return
        yield page
        start += PAGE_SIZE
        if total is not None and start >= total:
            return
        time.sleep(REQUEST_DELAY_S)


def _to_product(raw: dict) -> Product | None:
    items = raw.get("items") or []
    if not items:
        return None
    sellers = items[0].get("sellers") or []
    if not sellers:
        return None
    offer = sellers[0].get("commertialOffer") or {}
    price = offer.get("Price")
    if price is None:
        return None
    list_price = offer.get("ListPrice")
    images = items[0].get("images") or []

    # Cada item em items[] = uma variação de tamanho. Coletamos só as disponíveis.
    # OUS expõe a variação em it["variations"] = ["Numeração"] (ou "Tamanho" em
    # alguns produtos), e o valor real está em it[<nome-da-variation>] como list.
    # NÃO usar it["name"] porque vem com cor concatenada (ex: "P Branco").
    sizes: list[str] = []
    total_qty = 0
    any_qty_reported = False
    for it in items:
        sels = it.get("sellers") or []
        if not sels:
            continue
        co = sels[0].get("commertialOffer") or {}
        if not co.get("IsAvailable"):
            continue
        size_label: Optional[str] = None
        for var_name in (it.get("variations") or []):
            if not isinstance(var_name, str):
                continue
            vals = it.get(var_name)
            if isinstance(vals, list) and vals:
                size_label = str(vals[0]).strip()
                break
        if size_label:
            sizes.append(size_label)
        qty = co.get("AvailableQuantity")
        if qty is not None:
            any_qty_reported = True
            total_qty += int(qty)

    return Product(
        source="ous",
        sku=str(raw["productId"]),
        name=raw.get("productName") or raw.get("productTitle") or "",
        url=raw.get("link", ""),
        image=images[0].get("imageUrl") if images else None,
        list_price=float(list_price) if list_price else None,
        price=float(price),
        available=bool(offer.get("IsAvailable")),
        brand=raw.get("brand"),
        sizes=sizes,
        stock_qty=total_qty if any_qty_reported else None,
    )


class OusScraper:
    source = "ous"

    def fetch_all(self) -> list[Product]:
        out: list[Product] = []
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT_S, follow_redirects=True) as client:
            for page in _iter_pages(client):
                for raw in page:
                    p = _to_product(raw)
                    if p is not None:
                        out.append(p)
        log.info("OUS garimpo: %d produtos carregados", len(out))
        return out
