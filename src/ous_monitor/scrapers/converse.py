"""Scraper da categoria Sale da Converse Brasil (Magento 2).

A listagem é renderizada no servidor em blocos ``li.product-item`` e expõe:

* total de resultados em ``.toolbar-number`` e 36 itens por página;
* paginação padrão Magento via ``?p=N``;
* SKU, preços e imagem nos atributos do card;
* tamanhos/estoque no ``jsonConfig`` do renderer de swatches.

Os tamanhos retornados são somente os saláveis da cor exibida no card. Isso é
importante porque o filtro central de ingestão mantém apenas tênis com 42/43.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import math
import re
import time
from typing import Dict, Iterator, List, Optional, Tuple

import httpx
from selectolax.parser import HTMLParser

from ..models import Product

log = logging.getLogger(__name__)

BASE_URL = "https://converse.com.br/sale-c"
PAGE_SIZE = 36
REQUEST_DELAY_S = 0.5
TIMEOUT_S = 30.0
MAX_PAGES_HARD_CAP = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

_PRODUCT_ID_RE = re.compile(r"prod-list-(\d+)")


def _float_attr(node) -> Optional[float]:
    if node is None:
        return None
    raw = node.attributes.get("data-price-amount")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _available_sizes_by_product(tree: HTMLParser) -> Dict[str, List[str]]:
    """Extrai productId pai -> tamanhos saláveis da cor exibida no card."""
    result: Dict[str, List[str]] = {}
    for script in tree.css('script[type="text/x-magento-init"]'):
        text = html_lib.unescape(script.text() or "").strip()
        if "Magento_Swatches/js/swatch-renderer" not in text:
            continue
        try:
            init = json.loads(text)
        except json.JSONDecodeError:
            continue
        for component in init.values():
            if not isinstance(component, dict):
                continue
            renderer = component.get("Magento_Swatches/js/swatch-renderer")
            if not isinstance(renderer, dict):
                continue
            config = renderer.get("jsonConfig")
            if not isinstance(config, dict):
                continue
            product_id = str(config.get("productId") or "")
            attributes = config.get("attributes") or {}
            size_attr_id = ""
            color_attr_id = ""
            size_labels: Dict[str, str] = {}
            for attr_id, attr in attributes.items():
                if not isinstance(attr, dict):
                    continue
                if attr.get("code") == "size":
                    size_attr_id = str(attr_id)
                    size_labels = {
                        str(option.get("id")): str(option.get("label"))
                        for option in attr.get("options") or []
                        if option.get("id") is not None and option.get("label")
                    }
                elif attr.get("code") == "color":
                    color_attr_id = str(attr_id)
            if not product_id or not size_attr_id:
                continue

            selected_color = str(renderer.get("childColorIdPlp") or "")
            salable = config.get("salable") or {}
            variants: List[str] = []
            if color_attr_id and selected_color:
                variants = [
                    str(value)
                    for value in (salable.get(color_attr_id) or {}).get(selected_color, [])
                ]
            else:
                variants = [
                    str(value)
                    for values in (salable.get(size_attr_id) or {}).values()
                    for value in values
                ]

            index = config.get("index") or {}
            sizes = {
                size_labels.get(str((index.get(variant) or {}).get(size_attr_id)), "")
                for variant in variants
            }
            result[product_id] = sorted(size for size in sizes if size)
    return result


def _extract_page(html: str) -> Tuple[Optional[int], List[Product]]:
    tree = HTMLParser(html)
    total_node = tree.css_first(".toolbar-number")
    try:
        total = int((total_node.text(strip=True) if total_node else "").replace(".", ""))
    except ValueError:
        total = None

    sizes_by_product = _available_sizes_by_product(tree)
    products: List[Product] = []
    for card in tree.css("ol.product-items > li.product-item"):
        match = _PRODUCT_ID_RE.search(card.attributes.get("id", ""))
        product_id = match.group(1) if match else ""
        form = card.css_first("form[data-product-sku]")
        sku = form.attributes.get("data-product-sku", "") if form else ""
        name_node = card.css_first(".product-item-link")
        raw_name = name_node.text(strip=True) if name_node else ""
        url = name_node.attributes.get("href", "") if name_node else ""
        price = _float_attr(card.css_first('[data-price-type="finalPrice"]'))
        old_price = _float_attr(card.css_first('[data-price-type="oldPrice"]'))
        if not product_id or not sku or not raw_name or price is None or price <= 0:
            continue

        category_node = card.css_first(".category.gender_shoe-style")
        category = category_node.text(strip=True) if category_node else ""
        sizes = sizes_by_product.get(product_id, [])
        is_shoe = product_id in sizes_by_product or bool(category)
        display_name = f"Tênis Converse {raw_name}" if is_shoe else raw_name
        if category:
            display_name = f"{display_name} — {category}"

        image_node = card.css_first(".product-image-photo")
        image = None
        if image_node:
            image = image_node.attributes.get("data-src") or image_node.attributes.get("src")

        products.append(Product(
            source="converse",
            sku=sku,
            name=display_name,
            url=url,
            image=image,
            list_price=old_price if old_price is not None and old_price > price else None,
            price=price,
            available=True,
            brand="Converse",
            sizes=sizes,
            stock_qty=None,
        ))
    return total, products


def _iter_pages(client: httpx.Client) -> Iterator[List[Product]]:
    page = 1
    total_pages: Optional[int] = None
    while page <= MAX_PAGES_HARD_CAP:
        params = {} if page == 1 else {"p": page}
        response = client.get(BASE_URL, params=params)
        response.raise_for_status()
        total, products = _extract_page(response.text)
        if total_pages is None and total is not None:
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            log.info(
                "Converse Sale: total declarado = %s itens (%s páginas)",
                total, total_pages,
            )
        if not products:
            return
        yield products
        if total_pages is not None and page >= total_pages:
            return
        page += 1
        time.sleep(REQUEST_DELAY_S)


class ConverseScraper:
    source = "converse"

    def fetch_all(self) -> List[Product]:
        products: List[Product] = []
        seen = set()
        with httpx.Client(
            headers=HEADERS,
            timeout=TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            for page_products in _iter_pages(client):
                for product in page_products:
                    if product.sku in seen:
                        continue
                    seen.add(product.sku)
                    products.append(product)
        log.info(
            "Converse Sale: %d produtos carregados (%d com tamanho 42/43)",
            len(products),
            sum(bool({"42", "43"} & set(product.sizes)) for product in products),
        )
        return products
