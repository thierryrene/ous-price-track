"""Codec versionado para ``callback_data`` do Telegram.

Os botões antigos usavam ``:`` e continuam aceitos pelo servidor. Botões novos
usam ``1.<op>.<arg>`` para que mudanças futuras possam coexistir com mensagens
que já estão no histórico do Telegram.
"""
from __future__ import annotations

MAX_CALLBACK_DATA_BYTES = 64
CALLBACK_VERSION = 1
SEPARATOR = "."


def encode(operation: str, *args: object) -> str:
    parts = [str(CALLBACK_VERSION), str(operation), *(str(arg) for arg in args)]
    for part in parts:
        if not part or SEPARATOR in part:
            raise ValueError(f"parte inválida de callback_data: {part!r}")
        if not part.isascii():
            raise ValueError("callback_data deve carregar apenas códigos ASCII")
    data = SEPARATOR.join(parts)
    if len(data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
        raise ValueError(
            f"callback_data excede {MAX_CALLBACK_DATA_BYTES} bytes: {data!r}"
        )
    return data


def decode_to_legacy(data: str) -> str:
    """Valida e converte o formato novo para o roteamento legado com ``:``.

    O retorno compatível permite migrar os handlers de forma incremental e
    mantém funcionais botões enviados antes desta versão.
    """
    value = str(data)
    if len(value.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
        raise ValueError("callback_data excede o limite do Telegram")
    if not value.startswith(f"{CALLBACK_VERSION}{SEPARATOR}"):
        return value
    parts = value.split(SEPARATOR)
    if len(parts) < 2 or not all(parts):
        raise ValueError("callback_data malformado")
    return ":".join(parts[1:])


__all__ = [
    "CALLBACK_VERSION",
    "MAX_CALLBACK_DATA_BYTES",
    "decode_to_legacy",
    "encode",
]

