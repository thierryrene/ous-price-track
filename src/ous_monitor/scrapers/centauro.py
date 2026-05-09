"""Scraper para a Centauro (busca por OUS).

A Centauro está atrás de Akamai BMP — curl/httpx puros são bloqueados (403)
mesmo com headers perfeitos, e os endpoints VTEX foram fechados ao público.
A solução estável é Playwright headless reaproveitando uma sessão (mesmo
BrowserContext mantém os cookies _abck resolvidos pelo sensor JS) e
parseando o `__NEXT_DATA__` que vem embutido no HTML — assim não dependemos
do DOM render, só do JSON injetado.

Total real: ~184 itens na busca por "ous", ~6 páginas. Filtramos por
`details.brand == "Ous"` para descartar marketplace ruído.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Iterator, List, Optional

from ..models import Product

log = logging.getLogger(__name__)

BASE_URL = "https://www.centauro.com.br/busca/ous"
PAGE_DELAY_S = 6.0
NAV_TIMEOUT_MS = 45_000
MAX_PAGES_HARD_CAP = 15

# Use o Chrome instalado no sistema em vez de baixar 300MB do Playwright.
SYSTEM_CHROME = "/usr/bin/google-chrome"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
    re.DOTALL,
)


def _extract_next_data(html: str) -> Optional[dict]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    return json.loads(m.group(1))


def _find_products_blob(next_data: dict) -> tuple[List[dict], Optional[int], Optional[int]]:
    """Localiza a lista de produtos no __NEXT_DATA__.

    O caminho é props.pageProps.fallback['@"ous",@,'].products mas a chave
    do fallback varia ligeiramente entre páginas (o sufixo muda com filtros).
    Procuramos qualquer entrada de fallback que tenha .products.
    """
    fallback = (
        next_data.get("props", {}).get("pageProps", {}).get("fallback", {})
    )
    for value in fallback.values():
        if isinstance(value, dict) and "products" in value:
            return (
                value.get("products") or [],
                value.get("quantity"),
                (value.get("pagination") or {}).get("last", {}).get("pageNumber")
                if isinstance(value.get("pagination"), dict)
                else None,
            )
    return [], None, None


def _to_product(raw: dict) -> Optional[Product]:
    details = raw.get("details") or {}
    brand = details.get("brand") or raw.get("brand")
    if not brand or brand.strip().upper() != "OUS":
        return None
    price = raw.get("price")
    if price is None:
        return None
    img = raw.get("image") or {}
    image_url = img.get("default") if isinstance(img, dict) else None
    return Product(
        source="centauro",
        sku=str(raw.get("id") or raw.get("mainId") or ""),
        name=raw.get("name") or "",
        url=raw.get("url_absolute") or raw.get("url") or "",
        image=image_url,
        list_price=float(raw["oldPrice"]) if raw.get("oldPrice") else None,
        price=float(price),
        available=bool(raw.get("available", True)),
        brand=brand,
    )


class CentauroBlocked(RuntimeError):
    """Akamai bloqueou a requisição (403 Access Denied)."""


def _is_akamai_block(html: str, status: Optional[int]) -> bool:
    if status == 403:
        return True
    return "Access Denied" in html and "edgesuite" in html.lower()


def _proxy_config() -> Optional[dict]:
    """Lê CENTAURO_PROXY (ou HTTPS_PROXY/HTTP_PROXY) e devolve no formato Playwright.

    Aceita:
        http://host:port
        http://user:pass@host:port
        socks5://host:port
    """
    raw = (
        os.environ.get("CENTAURO_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not raw:
        return None
    # Playwright quer o servidor sem credenciais e user/password separados.
    from urllib.parse import urlparse

    parsed = urlparse(raw)
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    cfg: dict = {"server": server}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def _iter_pages() -> Iterator[List[dict]]:
    from playwright.sync_api import sync_playwright

    proxy = _proxy_config()
    if proxy:
        log.info("Centauro: usando proxy %s", proxy["server"])

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=SYSTEM_CHROME,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            proxy=proxy,
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()
        try:
            page_num = 1
            total_pages: Optional[int] = None
            while page_num <= MAX_PAGES_HARD_CAP:
                url = BASE_URL if page_num == 1 else f"{BASE_URL}?page={page_num}"
                log.info("Centauro: GET %s", url)
                resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                status = resp.status if resp else None
                html = page.content()
                if _is_akamai_block(html, status):
                    raise CentauroBlocked(
                        f"Akamai 403 na página {page_num}. IP/sessão queimados — "
                        "tente novamente daqui a horas, com VPN, ou via proxy residencial."
                    )
                next_data = _extract_next_data(html)
                if not next_data:
                    log.warning("Centauro p%d: __NEXT_DATA__ não encontrado", page_num)
                    return
                products, total, last_page = _find_products_blob(next_data)
                if total_pages is None and last_page:
                    total_pages = last_page
                    log.info("Centauro: total=%s, páginas=%s", total, total_pages)
                if not products:
                    return
                yield products
                if total_pages is not None and page_num >= total_pages:
                    return
                page_num += 1
                time.sleep(PAGE_DELAY_S)
        finally:
            context.close()
            browser.close()


class CentauroScraper:
    source = "centauro"

    def fetch_all(self) -> List[Product]:
        out: List[Product] = []
        try:
            for page_items in _iter_pages():
                for raw in page_items:
                    p = _to_product(raw)
                    if p is not None:
                        out.append(p)
        except CentauroBlocked as e:
            log.warning("Centauro bloqueado pelo Akamai — pulando esta fonte. %s", e)
            return []
        log.info("Centauro: %d produtos OUS carregados", len(out))
        return out
