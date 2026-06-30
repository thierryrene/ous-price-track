"""Classificação de "tipo de peça" a partir do nome do produto.

Fonte única usada pelo resumo de alta carga (`notifier.build_summary`) para
agrupar promoções por categoria. Cada produto cai em **exatamente um** bucket,
seguindo a ordem de prioridade de `CATEGORY_ORDER` (mais específico → genérico),
com `outros` como fallback.

A comparação é acento-insensitive e caixa-insensitive: tanto o nome quanto o
vocabulário são normalizados (NFKD, sem marcas diacríticas) antes do `in`.

Obs.: o filtro SQL por categoria do bot on-demand vive em
`services._category_sql` e é mantido à parte de propósito — mexer nele mudaria
o comportamento das varreduras filtradas. Aqui o objetivo é só agrupar para
exibição.
"""
from __future__ import annotations

import unicodedata

# Ordem = prioridade de classificação E ordem de exibição no resumo.
CATEGORY_ORDER = ("tenis", "camisas_time", "agasalhos", "acessorios", "vestuario", "outros")

CATEGORY_META = {
    "tenis": ("👟", "Tênis/Calçados"),
    "camisas_time": ("⚽", "Camisas de time"),
    "agasalhos": ("🧥", "Agasalhos"),
    "acessorios": ("🧢", "Acessórios"),
    "vestuario": ("👕", "Vestuário"),
    "outros": ("📦", "Outros"),
}

# Vocabulário por bucket (já acento-livre). camisas_time é tratado à parte
# porque exige "camisa" + um marcador de time.
_KEYWORDS = {
    "tenis": ["tenis", "chuteira", "chinelo", "sandalia", "bota",
              "sapatenis", "slide", "papete"],
    "agasalhos": ["agasalho", "moletom", "jaqueta", "corta vento", "corta-vento",
                  "windbreaker", "blusa", "sueter", "casaco", "colete"],
    "acessorios": ["bone", "gorro", "touca", "mochila", "shoulder", "bag",
                   "cinto", "oculos", "meia", "carteira", "necessaire",
                   "pochete", "luva", "cachecol"],
    "vestuario": ["camiseta", "camisa", "calca", "bermuda", "short", "polo",
                  "regata", "top", "legging", "saia", "vestido", "macacao",
                  "body", "cropped", "calcao"],
}

_TIME_TOKENS = ["time", "torcida", "selecao", "clube", "fan", "torcedor"]


def _strip(text) -> str:
    """lowercase + remove acentos (NFKD, descarta marcas combinantes)."""
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _any(haystack: str, needles) -> bool:
    return any(n in haystack for n in needles)


def categorize(name) -> str:
    """Devolve a chave de categoria (uma de `CATEGORY_ORDER`) para um nome."""
    n = _strip(name)
    if _any(n, _KEYWORDS["tenis"]):
        return "tenis"
    if "camisa" in n and _any(n, _TIME_TOKENS):
        return "camisas_time"
    if _any(n, _KEYWORDS["agasalhos"]):
        return "agasalhos"
    if _any(n, _KEYWORDS["acessorios"]):
        return "acessorios"
    if _any(n, _KEYWORDS["vestuario"]):
        return "vestuario"
    return "outros"


def category_label(key: str) -> str:
    emoji, label = CATEGORY_META.get(key, CATEGORY_META["outros"])
    return f"{emoji} {label}"
