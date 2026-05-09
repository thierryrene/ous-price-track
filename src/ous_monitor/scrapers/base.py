from typing import Protocol

from ..models import Product, Source


class Scraper(Protocol):
    source: Source

    def fetch_all(self) -> list[Product]:
        """Return every OUS product available on this source, fully paginated."""
        ...
