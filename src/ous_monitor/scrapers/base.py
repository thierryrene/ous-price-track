from __future__ import annotations

import json
from contextlib import contextmanager
from time import sleep
from typing import Iterator, Protocol
from urllib.parse import urljoin

import httpx

from ..models import Product, Source

DEFAULT_TIMEOUT_S = 30.0

DEFAULT_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


class Scraper(Protocol):
    source: Source

    def fetch_all(self) -> list[Product]:
        """Return every valid product available on this source.

        Invariants expected by filters/storage:
        - source matches the registry key.
        - sku, name, url and price are non-empty.
        - price is in BRL units, not cents.
        - list_price is None or greater than price.
        - sizes contains only available sizes; empty means the source does not expose it.
        - structural parse failures should be logged or raised, not silently treated
          as an empty catalog unless the source explicitly skipped due to blocking.
        """
        ...


@contextmanager
def browser_client(*, headers: dict[str, str] | None = None,
                   timeout: float = DEFAULT_TIMEOUT_S,
                   follow_redirects: bool = True) -> Iterator[httpx.Client]:
    merged = dict(DEFAULT_BROWSER_HEADERS)
    if headers:
        merged.update(headers)
    with httpx.Client(headers=merged, timeout=timeout,
                      follow_redirects=follow_redirects) as client:
        yield client


def page_delay(seconds: float) -> None:
    if seconds > 0:
        sleep(seconds)


def absolute_url(base_url: str, url: str | None) -> str:
    return urljoin(base_url, url or "")


def normalize_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("R$", "").replace(".", "").replace(",", ".").strip()
    try:
        return float(text)
    except ValueError:
        return None


def valid_list_price(list_price: float | None, price: float) -> float | None:
    return list_price if list_price is not None and list_price > price else None


def parse_json_after_marker(text: str, marker: str) -> dict | list | None:
    start = text.find(marker)
    if start < 0:
        return None
    start = text.find("{", start)
    if start < 0:
        start = text.find("[", text.find(marker))
    if start < 0:
        return None

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escape_next = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:idx + 1])
    return None
