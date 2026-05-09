"""Scraper para o Clube Netshoes (preços de assinante para a marca OUS).

Estratégia: clube.netshoes.com.br retorna 200 com User-Agent de browser.
A listagem da marca está em /busca?q=ous&marca=ous&page=N (5 páginas, 42/pg,
~204 produtos no total). Os dados completos vivem em window.__INITIAL_STATE__,
um JSON ~316KB injetado no fim do HTML.

Pegadinhas (descobertas na investigação):
- A marca canônica é "ÖUS" (Ö com umlaut), não "OUS".
- Preços vêm em CENTAVOS como int — dividir por 100.
- Paginação é ?page=N, NÃO ?p=N (este é silenciosamente ignorado).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterator, List, Optional

import httpx

from ..models import Product

log = logging.getLogger(__name__)

BASE = "https://clube.netshoes.com.br"
SEARCH_PATH = "/busca"
SEARCH_PARAMS = {"q": "ous", "marca": "ous"}
REQUEST_DELAY_S = 1.5
TIMEOUT_S = 30.0
MAX_PAGES_HARD_CAP = 20  # safety net; real total ~5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")


def _extract_initial_state(html: str) -> Optional[dict]:
    """Find `window.__INITIAL_STATE__ = {...};` and return the parsed object.

    Greedy regex doesn't work because the JSON itself contains "};", so we
    do a proper brace-balanced scan that respects strings and escapes.
    """
    match = _INITIAL_STATE_RE.search(html)
    if not match:
        return None
    start = match.end()
    if start >= len(html) or html[start] != "{":
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : i + 1])
    return None


def _is_ous(brand: Optional[str]) -> bool:
    if not brand:
        return False
    return brand.strip().upper().replace("Ö", "O") == "OUS"


def _to_product(raw: dict) -> Optional[Product]:
    if not _is_ous(raw.get("brand")):
        return None
    sale_cents = raw.get("salePrice")
    if sale_cents is None:
        return None
    list_cents = raw.get("listPrice")
    slug = raw.get("productSlug") or ""
    url = (BASE + slug) if slug.startswith("/") else slug
    sizes_raw = raw.get("sizes") or []
    sizes = [str(s).strip() for s in sizes_raw if str(s).strip()]
    return Product(
        source="netshoes",
        sku=str(raw.get("code") or raw.get("productCode") or ""),
        name=raw.get("name") or "",
        url=url,
        image=raw.get("image"),
        list_price=float(list_cents) / 100 if list_cents else None,
        price=float(sale_cents) / 100,
        available=bool(raw.get("available", True)),
        brand=raw.get("brand"),
        sizes=sizes,
        stock_qty=None,  # Netshoes não expõe qty na listagem
    )


def _iter_pages(client: httpx.Client) -> Iterator[List[dict]]:
    page = 1
    total_pages: Optional[int] = None
    while page <= MAX_PAGES_HARD_CAP:
        params = {**SEARCH_PARAMS, "page": page}
        resp = client.get(SEARCH_PATH, params=params)
        resp.raise_for_status()
        state = _extract_initial_state(resp.text)
        if not state:
            log.warning("Netshoes p%d: __INITIAL_STATE__ não encontrado", page)
            return
        search = (state.get("SearchPage") or {})
        items = search.get("parentSkus") or []
        if total_pages is None:
            total_pages = search.get("totalPages")
            log.info("Netshoes: total declarado = %s itens, %s páginas",
                     search.get("total"), total_pages)
        if not items:
            return
        yield items
        if total_pages is not None and page >= total_pages:
            return
        page += 1
        time.sleep(REQUEST_DELAY_S)


class NetshoesScraper:
    source = "netshoes"

    def fetch_all(self) -> List[Product]:
        out: List[Product] = []
        with httpx.Client(
            base_url=BASE, headers=HEADERS, timeout=TIMEOUT_S, follow_redirects=True,
        ) as client:
            for page_items in _iter_pages(client):
                for raw in page_items:
                    p = _to_product(raw)
                    if p is not None:
                        out.append(p)
        log.info("Netshoes: %d produtos OUS carregados", len(out))
        return out
