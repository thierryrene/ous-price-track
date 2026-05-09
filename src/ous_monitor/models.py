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
