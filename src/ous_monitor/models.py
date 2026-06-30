from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

Source = str  # "ous" | "centauro" | "netshoes"


@dataclass(frozen=True)
class Product:
    source: Source
    sku: str
    name: str
    url: str
    image: Optional[str]
    list_price: Optional[float]
    price: float
    available: bool
    brand: Optional[str] = None
    # Tamanhos disponíveis (somente os com estoque). Strings já normalizadas
    # ("39", "39.5", "P", "M", "ÚNICO"). Pode estar vazia se a fonte não expõe.
    sizes: List[str] = field(default_factory=list)
    # Quantidade total em estoque somando variações disponíveis. None se
    # a fonte não reporta (Netshoes/Centauro listagem).
    stock_qty: Optional[int] = None

    @property
    def has_discount(self) -> bool:
        return self.list_price is not None and self.list_price > self.price

    @property
    def discount_pct(self) -> float:
        if not self.has_discount:
            return 0.0
        return round((1 - self.price / self.list_price) * 100, 1)

    @property
    def savings(self) -> float:
        if not self.has_discount:
            return 0.0
        return float(self.list_price) - self.price

    @property
    def low_stock(self) -> bool:
        """OR dos critérios: <=2 tamanhos disponíveis OU <=3 unidades."""
        few_sizes = 0 < len(self.sizes) <= 2
        few_units = self.stock_qty is not None and 0 < self.stock_qty <= 3
        return few_sizes or few_units


@dataclass(frozen=True)
class RunCounters:
    new: int = 0
    updated: int = 0
    price_drop: int = 0
    new_promo: int = 0

    @classmethod
    def from_mapping(cls, values: dict | None) -> "RunCounters":
        values = values or {}
        return cls(
            new=int(values.get("new", 0)),
            updated=int(values.get("updated", 0)),
            price_drop=int(values.get("price_drop", 0)),
            new_promo=int(values.get("new_promo", 0)),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "new": self.new,
            "updated": self.updated,
            "price_drop": self.price_drop,
            "new_promo": self.new_promo,
        }


@dataclass(frozen=True)
class RunResult:
    counters: RunCounters

    def as_dict(self) -> dict[str, int]:
        return self.counters.as_dict()


@dataclass(frozen=True)
class ChangeSet:
    new_promo: list
    ended: list
    weaker: list
    price_up: list

    def as_dict(self) -> dict[str, list]:
        return {
            "new_promo": self.new_promo,
            "ended": self.ended,
            "weaker": self.weaker,
            "price_up": self.price_up,
        }


@dataclass(frozen=True)
class PromotionChange:
    source: str
    sku: str
    name: str
    url: str
    image: Optional[str]
    list_price: Optional[float]
    price: float
    observed_at: str
    prev_price: Optional[float] = None
    prev_list_price: Optional[float] = None
    prev_observed_at: Optional[str] = None
    sizes: Optional[str] = None
    stock_qty: Optional[int] = None
