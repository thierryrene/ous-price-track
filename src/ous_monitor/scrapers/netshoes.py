"""Scraper para o Clube Netshoes.

Estratégia: clube.netshoes.com.br retorna 200 com User-Agent de browser. A
listagem de uma marca está em /busca?q=<q>&marca=<slug>&page=N e os dados
completos vivem em `window.__INITIAL_STATE__` (JSON injetado no HTML).

Cada instância é parametrizada por marca — o mesmo código serve OUS, BaW,
etc. — bastando passar `source_name`, `brand_query`, `brand_marca` e um
`brand_matcher` que confirma a marca do produto retornado.

Pegadinhas (descobertas na investigação):
- ÖUS: marca canônica tem Ö com umlaut; slug é `marca=ous`.
- BaW Clothing: brand label vem como "BAW Clothing"; slug é `marca=baw-clothing`
  (apenas `marca=baw` retorna outra marca "BAW" genérica com 2 itens).
- Preços vêm em CENTAVOS como int — dividir por 100.
- Paginação é ?page=N, NÃO ?p=N (este é silenciosamente ignorado).
- Produtos ficam em `SearchPage.parentSkus` (não `SearchPage.products`).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Iterator, List, Optional

import httpx

from ..models import Product

log = logging.getLogger(__name__)

BASE = "https://clube.netshoes.com.br"
SEARCH_PATH = "/busca"
REQUEST_DELAY_S = 1.5
TIMEOUT_S = 30.0
MAX_PAGES_HARD_CAP = 200  # safety net (Adidas tem ~164 páginas; ÖUS ~5, BaW ~2)

# Netshoes rate-limita (429) IPs compartilhados — runners de CI especialmente.
# Em vez de falhar de imediato, repetimos com backoff exponencial, respeitando
# o header Retry-After quando presente.
RETRY_STATUSES = {429, 503}
MAX_RETRIES = 4
BACKOFF_BASE_S = 3.0
MAX_BACKOFF_S = 60.0

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
    "Accept-Encoding": "gzip, deflate",
}

_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")


def _retry_after_seconds(resp: "httpx.Response") -> Optional[float]:
    """Lê o header Retry-After (segundos). Ignora formato de data HTTP."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (TypeError, ValueError):
        return None


def _get_with_retry(client: httpx.Client, params: dict) -> "httpx.Response":
    """GET com backoff exponencial em 429/503 (respeitando Retry-After).

    Após esgotar as tentativas, propaga o erro via raise_for_status (a fonte
    é registrada como 'failed', sem derrubar as demais)."""
    for attempt in range(MAX_RETRIES + 1):
        resp = client.get(SEARCH_PATH, params=params)
        if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
            wait = _retry_after_seconds(resp)
            if wait is None:
                wait = BACKOFF_BASE_S * (2 ** attempt)
            wait = min(wait, MAX_BACKOFF_S)
            log.warning(
                "netshoes: HTTP %d (rate-limit) em %s; aguardando %.1fs "
                "(tentativa %d/%d)",
                resp.status_code, params, wait, attempt + 1, MAX_RETRIES,
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()  # última resposta ainda em erro — propaga
    return resp


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


def _is_baw(brand: Optional[str]) -> bool:
    if not brand:
        return False
    # Aceita "BAW Clothing", "BAW CLOTHING", "Baw Clothing" — mas rejeita
    # "BAW" puro (que na Netshoes é outra marca genérica de 2 itens).
    return brand.strip().upper() == "BAW CLOTHING"


def _is_adidas(brand: Optional[str]) -> bool:
    if not brand:
        return False
    # Strict: aceita só "Adidas". "Adidas Originals" é catalogada como linha
    # separada na Netshoes (marca=adidas-originals) e não vem em marca=adidas.
    return brand.strip().upper() == "ADIDAS"


def _is_adidas_originals(brand: Optional[str]) -> bool:
    if not brand:
        return False
    return brand.strip().upper() == "ADIDAS ORIGINALS"


class NetshoesScraper:
    """Scraper parametrizado por marca. Default = ÖUS (compat com versão antiga)."""

    def __init__(
        self,
        source_name: str = "netshoes",
        brand_query: str = "ous",
        brand_marca: str = "ous",
        brand_matcher: Callable[[Optional[str]], bool] = _is_ous,
        brand_label: str = "OUS",
    ):
        self.source = source_name
        self._params = {"q": brand_query, "marca": brand_marca}
        self._matches_brand = brand_matcher
        self._brand_label = brand_label

    def _to_product(self, raw: dict) -> Optional[Product]:
        if not self._matches_brand(raw.get("brand")):
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
            source=self.source,
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

    def _iter_pages(self, client: httpx.Client) -> Iterator[List[dict]]:
        page = 1
        total_pages: Optional[int] = None
        while page <= MAX_PAGES_HARD_CAP:
            resp = _get_with_retry(client, {**self._params, "page": page})
            state = _extract_initial_state(resp.text)
            if not state:
                log.warning("%s p%d: __INITIAL_STATE__ não encontrado",
                            self.source, page)
                return
            search = (state.get("SearchPage") or {})
            items = search.get("parentSkus") or []
            if total_pages is None:
                total_pages = search.get("totalPages")
                log.info("%s: total declarado = %s itens, %s páginas",
                         self.source, search.get("total"), total_pages)
            if not items:
                return
            yield items
            if total_pages is not None and page >= total_pages:
                return
            page += 1
            time.sleep(REQUEST_DELAY_S)

    def fetch_all(self) -> List[Product]:
        out: List[Product] = []
        with httpx.Client(
            base_url=BASE, headers=HEADERS, timeout=TIMEOUT_S, follow_redirects=True,
        ) as client:
            for page_items in self._iter_pages(client):
                for raw in page_items:
                    p = self._to_product(raw)
                    if p is not None:
                        out.append(p)
        log.info("%s: %d produtos %s carregados",
                 self.source, len(out), self._brand_label)
        return out


def NetshoesBawScraper() -> NetshoesScraper:
    """Factory: Clube Netshoes filtrado por BaW Clothing."""
    return NetshoesScraper(
        source_name="netshoes_baw",
        brand_query="baw",
        brand_marca="baw-clothing",
        brand_matcher=_is_baw,
        brand_label="BAW Clothing",
    )


def NetshoesAdidasScraper() -> NetshoesScraper:
    """Factory: Clube Netshoes filtrado por Adidas (linha regular).

    Catálogo grande (~6900 itens, ~164 páginas). NÃO inclui Adidas Originals,
    que é catalogada à parte (marca=adidas-originals, ~92 itens).
    """
    return NetshoesScraper(
        source_name="netshoes_adidas",
        brand_query="adidas",
        brand_marca="adidas",
        brand_matcher=_is_adidas,
        brand_label="Adidas",
    )


def NetshoesAdidasOriginalsScraper() -> NetshoesScraper:
    """Factory: Clube Netshoes filtrado por Adidas Originals.

    Catálogo menor (~92 itens, ~4 páginas). Separado do Adidas regular
    (marca=adidas-originals vs marca=adidas).
    """
    return NetshoesScraper(
        source_name="netshoes_adidas_originals",
        brand_query="adidas originals",
        brand_marca="adidas-originals",
        brand_matcher=_is_adidas_originals,
        brand_label="Adidas Originals",
    )
