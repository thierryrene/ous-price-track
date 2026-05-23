"""Scraper para bawclothing.com.br (marca BaW Clothing).

A loja roda na plataforma Wake/FBits (header `x-powered-by: Wake`). Não é VTEX —
as APIs `/api/catalog_system/...` retornam 404. Mas a listagem `/roupas/?pagina=N`
é renderizada server-side e injeta dois blocos JSON úteis:

  1. <script type=application/ld+json> com `@type: "ItemList"`:
        { "numberOfItems": "587", "itemListElement": [
              { "@type": "Product", "url": "...", "image": "...",
                "name": "...", "offers": { "price": "139.0" } }, ... ] }
     Traz url, image, nome e preço corrente (sem oldPrice).

  2. Bloco JS inline `{item_list_name:"Hotsite products", items:[ ... ]}` com,
     por produto: `item_id` (numérico), `item_name`, `price`, `discount` e até
     4 níveis de `item_category*`. ATENÇÃO: `discount` aqui é o **valor absoluto
     em reais economizado**, NÃO o percentual. Logo list_price = price + discount
     (validado contra produtos reais: camiseta com price=89, discount=20 →
     list_price=109, coerente; tratar como % daria 111, errado por arredondamento
     e em itens caros explodiria — uma camiseta "de R$690" seria absurdo).

Casamos os dois blocos por `item_id`, que aparece como sufixo numérico no slug
da URL do JSON-LD (`.../tank-top-baw-ent-153686`).

Paginação: `/roupas/?pagina=N&tamanho=24` (passar APENAS `pagina` é silencio-
samente ignorado — sempre devolve a pg 1; o `tamanho` é o que destrava). Total
declarado no campo `numberOfItems` do JSON-LD. ~25 páginas em maio/2026.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Dict, Iterator, List, Optional, Tuple

import httpx

from ..models import Product

log = logging.getLogger(__name__)

BASE = "https://www.bawclothing.com.br"
LISTING_PATH = "/roupas/"
PAGE_SIZE = 24
REQUEST_DELAY_S = 1.0
TIMEOUT_S = 30.0
MAX_PAGES_HARD_CAP = 60  # safety net; real total ~25

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
    # NOTA: NÃO pedir brotli — httpx só decodifica br se o pacote opcional
    # `brotli`/`brotlicffi` estiver instalado, e o BaW sempre escolhe br quando
    # oferecido. gzip funciona out-of-the-box.
    "Accept-Encoding": "gzip, deflate",
}

_LD_JSON_RE = re.compile(
    r"<script\s+type=application/ld\+json\s*>(.*?)</script>", re.S
)
_DATALAYER_NEEDLE = '{item_list_name:"Hotsite products"'
# Para converter o literal JS do dataLayer em JSON: aspear chaves bare.
_JS_BARE_KEY_RE = re.compile(r'([\{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:')
# Extrai o id numérico do final do slug ("...-153686" → 153686).
_SLUG_ID_RE = re.compile(r"-(\d{4,})/?$")


def _parse_itemlist(html: str) -> Tuple[Optional[int], List[dict]]:
    """Devolve (numberOfItems_total, lista_de_produtos_da_pagina)."""
    for block in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "ItemList":
            continue
        total_raw = data.get("numberOfItems")
        try:
            total = int(total_raw) if total_raw is not None else None
        except (TypeError, ValueError):
            total = None
        return total, list(data.get("itemListElement") or [])
    return None, []


def _parse_hotsite_datalayer(html: str) -> Dict[int, dict]:
    """Extrai o objeto JS `{item_list_name:"Hotsite products", items:[...]}`
    via varredura brace-balanced e devolve dict item_id → item."""
    i = html.find(_DATALAYER_NEEDLE)
    if i < 0:
        return {}
    depth = 0
    in_str = False
    escape = False
    end = -1
    for j in range(i, len(html)):
        c = html[j]
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
                end = j + 1
                break
    if end < 0:
        return {}
    js_literal = html[i:end]
    json_text = _JS_BARE_KEY_RE.sub(r'\1"\2":', js_literal)
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        log.warning("BaW: falhou parsear data_layer hotsite: %s", e)
        return {}
    out: Dict[int, dict] = {}
    for item in data.get("items") or []:
        iid = item.get("item_id")
        if isinstance(iid, int):
            out[iid] = item
    return out


def _slug_id(url: str) -> Optional[int]:
    m = _SLUG_ID_RE.search(url.rstrip("/"))
    return int(m.group(1)) if m else None


def _to_product(ld_item: dict, dl_item: Optional[dict]) -> Optional[Product]:
    url = ld_item.get("url") or ""
    iid = _slug_id(url)
    if iid is None:
        return None
    offer = ld_item.get("offers") or {}
    try:
        price = float(offer.get("price"))
    except (TypeError, ValueError):
        return None
    discount_brl = 0.0
    if dl_item:
        try:
            discount_brl = float(dl_item.get("discount") or 0)
        except (TypeError, ValueError):
            discount_brl = 0.0
    if discount_brl > 0:
        list_price: Optional[float] = round(price + discount_brl, 2)
    else:
        list_price = None
    return Product(
        source="baw",
        sku=str(iid),
        name=ld_item.get("name") or (dl_item or {}).get("item_name") or "",
        url=url,
        image=ld_item.get("image"),
        list_price=list_price,
        price=price,
        available=True,  # listagem só mostra disponíveis
        brand="BaW Clothing",
        sizes=[],        # listagem não expõe tamanhos individuais
        stock_qty=None,  # idem
    )


def _iter_pages(client: httpx.Client) -> Iterator[Tuple[List[dict], Dict[int, dict]]]:
    page = 1
    total_items: Optional[int] = None
    page_size_seen: Optional[int] = None
    while page <= MAX_PAGES_HARD_CAP:
        # IMPORTANTE: o parâmetro `pagina` é silenciosamente ignorado se
        # `tamanho` não for enviado junto — a página 1 é devolvida em todos
        # os casos. Passar ambos é o que destrava a paginação SSR.
        resp = client.get(LISTING_PATH, params={"pagina": page, "tamanho": PAGE_SIZE})
        resp.raise_for_status()
        total, ld_items = _parse_itemlist(resp.text)
        dl_items = _parse_hotsite_datalayer(resp.text)
        if total_items is None and total is not None:
            total_items = total
            log.info("BaW: total declarado = %s itens", total_items)
        if page_size_seen is None and ld_items:
            page_size_seen = len(ld_items)
        if not ld_items:
            return
        yield ld_items, dl_items
        if total_items is not None and page_size_seen:
            if page * page_size_seen >= total_items:
                return
        page += 1
        time.sleep(REQUEST_DELAY_S)


class BawScraper:
    source = "baw"

    def fetch_all(self) -> List[Product]:
        out: List[Product] = []
        seen: set = set()
        with httpx.Client(
            base_url=BASE, headers=HEADERS, timeout=TIMEOUT_S, follow_redirects=True,
        ) as client:
            for ld_items, dl_items in _iter_pages(client):
                for ld in ld_items:
                    iid = _slug_id(ld.get("url") or "")
                    if iid is None or iid in seen:
                        continue
                    p = _to_product(ld, dl_items.get(iid))
                    if p is not None:
                        seen.add(iid)
                        out.append(p)
        promos = sum(1 for p in out if p.has_discount)
        log.info("BaW: %d produtos carregados (%d em promoção)", len(out), promos)
        return out
