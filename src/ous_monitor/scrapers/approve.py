"""Scraper para a loja Approve (justapprove.com.br).

Plataforma Tiendanube/Nube. A página /sale/ contém os produtos em promoção.
Os produtos são renderizados server-side no HTML. Paginação via ?page=N.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterator

import httpx
from selectolax.parser import HTMLParser

from ..models import Product

log = logging.getLogger(__name__)

BASE_URL = "https://www.justapprove.com.br/sale/"
REQUEST_DELAY_S = 1.0
TIMEOUT_S = 20.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def _extract_products_from_html(html: str) -> list[dict]:
    """Extract product data from Tiendanube HTML page.

    Tiendanube embeds product data in JavaScript variables and in
    the HTML structure. We parse both sources.

    Out-of-stock products are identified by the absence of an
    add-to-cart button (.js-addtocart-placeholder or .js-prod-submit-form).
    """
    products = []

    # Parse from HTML product cards
    tree = HTMLParser(html)
    for card in tree.css('.js-item-product'):
        sku = card.attributes.get('data-product-id', '')
        name_el = card.css_first('.js-item-name')
        name = name_el.text(strip=True) if name_el else ''

        # Check if product has add-to-cart button (in stock indicator)
        add_to_cart = card.css_first('.js-addtocart-placeholder, .js-prod-submit-form')
        if not add_to_cart:
            continue  # Skip sold out products

        # Get URL
        link_el = card.css_first('a[href]')
        url = link_el.attributes.get('href', '') if link_el else ''

        # Get prices from the card
        price = 0.0
        list_price = None

        # Try to get prices from data attributes or price elements
        price_el = card.css_first('.js-price-display, .price-display, [data-price]')
        if price_el:
            price_text = price_el.text(strip=True)
            price = _parse_price(price_text)

        list_price_el = card.css_first('.js-compare-price-display, .compare-price-display, .price-compare')
        if list_price_el:
            list_price_text = list_price_el.text(strip=True)
            list_price = _parse_price(list_price_text)
            if list_price and list_price <= price:
                list_price = None

        # Extract size from variant info if available
        sizes = []
        variant_el = card.css_first('.js-variant-label, .variant-label')
        if variant_el:
            variant_text = variant_el.text(strip=True)
            if variant_text:
                sizes.append(variant_text)

        if sku and name and price > 0:
            products.append({
                "sku": sku,
                "name": name,
                "price": price,
                "list_price": list_price,
                "url": url,
                "brand": "Approve",
                "sizes": sizes,
            })

    return products


def _parse_price(text: str | None) -> float:
    """Parse Brazilian price format (R$ 1.234,56) to float."""
    if not text:
        return 0.0
    # Remove R$ and whitespace
    cleaned = text.replace('R$', '').strip()
    # Remove thousands separator (.) and replace decimal comma with dot
    cleaned = cleaned.replace('.', '').replace(',', '.')
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _get_total_pages(html: str) -> int:
    """Extract total pages from pagination or product count."""
    # Try to get from LS.productsCount
    import re
    count_match = re.search(r'LS\.productsCount\s*=\s*(\d+)', html)
    if count_match:
        total_products = int(count_match.group(1))
        # Tiendanube shows 12 products per page
        return (total_products + 11) // 12
    return 20  # Default max pages


def _to_product(raw: dict) -> Product | None:
    """Convert raw product dict to Product model."""
    sku = raw.get("sku", "")
    name = raw.get("name", "")
    if not sku or not name:
        return None

    price = raw.get("price", 0)
    if price <= 0:
        return None

    list_price = raw.get("list_price")
    if list_price and list_price <= price:
        list_price = None

    url = raw.get("url", "")
    if url and not url.startswith("http"):
        url = "https://www.justapprove.com.br" + url

    # Get sizes from raw data
    sizes = raw.get("sizes", [])

    return Product(
        source="approve",
        sku=str(sku),
        name=name,
        url=url,
        image=None,
        list_price=float(list_price) if list_price else None,
        price=float(price),
        available=True,
        brand=raw.get("brand", "Approve"),
        sizes=sizes,
        stock_qty=None,
    )


def _iter_pages(client: httpx.Client) -> Iterator[list[dict]]:
    """Iterate through all pages of the sale category."""
    page = 1
    total_pages = None

    while True:
        url = BASE_URL if page == 1 else f"{BASE_URL}?page={page}"
        resp = client.get(url)
        resp.raise_for_status()

        html = resp.text

        if total_pages is None:
            total_pages = _get_total_pages(html)
            log.info("Approve: total de páginas declarado = %s", total_pages)

        products = _extract_products_from_html(html)
        if not products:
            return

        yield products

        if total_pages is not None and page >= total_pages:
            return
        if total_pages is None and len(products) == 0:
            return

        page += 1
        time.sleep(REQUEST_DELAY_S)


class ApproveScraper:
    source = "approve"

    def fetch_all(self) -> list[Product]:
        out: list[Product] = []
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT_S, follow_redirects=True) as client:
            for page_products in _iter_pages(client):
                for raw in page_products:
                    p = _to_product(raw)
                    if p is not None:
                        out.append(p)
        log.info("Approve: %d produtos carregados", len(out))
        return out
