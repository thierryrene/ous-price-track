"""Classificador heurístico de gênero/idade do produto, baseado no nome.

Usado para filtrar notificações: queremos apenas peças masculinas ou
sem marcador de gênero (interpretadas como unissex). Rejeita explicitamente
feminino-exclusivos e infantil/juvenil.

A heurística olha só o nome (cross-platform, simples). Acerta os casos óbvios:
- "Tênis Adidas Galaxar Feminino" → False (feminino)
- "Tênis Adidas Forum Junior"     → False (kids)
- "Calcinha BaW"                  → False (categoria fem)
- "Camiseta Oversized BAW"        → True (sem marcador → trata como unissex)
- "Bermuda Adidas Masculino"      → True
- "BIQUINI TOP RIB CANDY"         → False (biquíni)

Quando há ambiguidade (sem marcador algum), aceita — coerente com a postura
"melhor mostrar do que perder" do filtro 42/43.
"""
from __future__ import annotations

import re
import unicodedata

# Marcadores explícitos: presença de qualquer um destes tokens rejeita o item.
# Todos comparados em forma normalizada (sem acento, minúscula, word-boundary).
_BLOCK_TOKENS = frozenset({
    # Gênero feminino
    "feminino", "feminina", "femininas", "femininos", "fem",
    "mulher", "mulheres", "garota", "garotas", "girl", "girls",
    "women", "womens", "ladies", "wmn", "wmns", "she", "her",
    # Idade infantil/juvenil
    "infantil", "infantis", "juvenil", "juvenis", "kids", "kid",
    "junior", "juniors", "menino", "menina", "meninos", "meninas",
    "bebe", "baby", "babys", "babies", "crianca", "criancas",
    # Maternidade
    "maternidade", "gestante", "gestantes", "amamentacao",
    # Categorias femininas-exclusivas
    "calcinha", "calcinhas", "biquini", "biquinis", "sutia", "sutias",
    "lingerie", "vestido", "vestidos", "saia", "saias",
    "camisola", "camisolas",
})

# Regex de tokenização: sequências alfabéticas (já lidamos com acentos antes).
_TOKEN_RE = re.compile(r"[a-z]+")


def _normalize(s: str) -> str:
    """Lowercase + sem acento (NFD + remove combining marks)."""
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if not unicodedata.combining(c)).lower()


def is_male_or_unisex(name: str) -> bool:
    """True se o nome NÃO contém marcador feminino ou infantil/juvenil."""
    if not name:
        return True
    tokens = set(_TOKEN_RE.findall(_normalize(name)))
    return not (tokens & _BLOCK_TOKENS)
