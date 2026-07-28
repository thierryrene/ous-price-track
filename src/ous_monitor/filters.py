"""Filtros de ingestão — decidem se um produto entra (ou permanece) no DB.

Combina dois critérios:
  1. Gênero/idade (`gender.is_male_or_unisex`): rejeita feminino-exclusivo,
     infantil/juvenil, maternidade, categorias femininas.
  2. Tamanho por categoria: calçados precisam ter 42/43 e roupas precisam ter
     M/G/GG entre os `sizes` disponíveis. Grade vazia reprova esses produtos;
     acessórios e itens sem categoria de tamanho continuam passando.

A mesma lógica é usada em dois pontos:
  * ingestão (`cli._scrape_and_persist`) — filtra antes de `record_run`
  * purge (`cli.cmd_purge`) — remove do DB rows que falham hoje

Usar `should_keep` (uniforme via name+sizes_iter) garante que ambos os pontos
batem o mesmo critério.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Tuple

from .categories import categorize
from .gender import is_male_or_unisex
from .models import Product

SHOE_SIZES_WANTED = frozenset({"42", "43"})
CLOTHING_SIZES_WANTED = frozenset({"M", "G", "GG"})

_TENIS_RE = re.compile(r"\btenis\b")


def is_tenis(name: str) -> bool:
    """True se o nome do produto descreve um tênis (acento-insensitive)."""
    norm = unicodedata.normalize("NFD", name or "").encode("ascii", "ignore").decode().lower()
    return bool(_TENIS_RE.search(norm))


def passes_size_filter(name: str, sizes: Iterable[str]) -> bool:
    """Aplica grade estrita a calçados e roupas; acessórios não têm grade."""
    sizes_set = {
        str(size).strip().upper()
        for size in sizes
        if size and str(size).strip()
    }
    category = categorize(name)
    if category == "tenis":
        return bool(sizes_set & SHOE_SIZES_WANTED)
    if category in {"camisas_time", "agasalhos", "vestuario"}:
        return bool(sizes_set & CLOTHING_SIZES_WANTED)
    return True


def should_keep(name: str, sizes: Iterable[str]) -> Tuple[bool, str]:
    """Aplica gênero antes de tamanho. Devolve (keep?, motivo_se_rejeitar)."""
    if not is_male_or_unisex(name or ""):
        return False, "gender"
    if not passes_size_filter(name or "", sizes):
        return False, "size"
    return True, ""


def should_keep_product(p: Product) -> Tuple[bool, str]:
    return should_keep(p.name, p.sizes or ())
