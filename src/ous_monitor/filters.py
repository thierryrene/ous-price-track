"""Filtros de ingestão — decidem se um produto entra (ou permanece) no DB.

Combina dois critérios:
  1. Gênero/idade (`gender.is_male_or_unisex`): rejeita feminino-exclusivo,
     infantil/juvenil, maternidade, categorias femininas.
  2. Tamanho de tênis: itens cujo nome contém `\btênis\b` precisam ter 42 ou 43
     entre os `sizes` disponíveis. Tênis com `sizes` vazio passa direto
     (BaW oficial não expõe tamanhos na listagem — melhor mostrar do que perder).

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

from .gender import is_male_or_unisex
from .models import Product

SHOE_SIZES_WANTED = frozenset({"42", "43"})

_TENIS_RE = re.compile(r"\btenis\b")


def is_tenis(name: str) -> bool:
    """True se o nome do produto descreve um tênis (acento-insensitive)."""
    norm = unicodedata.normalize("NFD", name or "").encode("ascii", "ignore").decode().lower()
    return bool(_TENIS_RE.search(norm))


def passes_size_filter(name: str, sizes: Iterable[str]) -> bool:
    """True para não-tênis, ou tênis com 42/43 disponível, ou tênis sem sizes."""
    if not is_tenis(name):
        return True
    sizes_set = {s.strip() for s in sizes if s and str(s).strip()}
    if not sizes_set:
        return True  # safety: melhor mostrar do que perder
    return bool(sizes_set & SHOE_SIZES_WANTED)


def should_keep(name: str, sizes: Iterable[str]) -> Tuple[bool, str]:
    """Aplica gênero antes de tamanho. Devolve (keep?, motivo_se_rejeitar)."""
    if not is_male_or_unisex(name or ""):
        return False, "gender"
    if not passes_size_filter(name or "", sizes):
        return False, "size"
    return True, ""


def should_keep_product(p: Product) -> Tuple[bool, str]:
    return should_keep(p.name, p.sizes or ())
