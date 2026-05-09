"""Envio de notificações de promoções novas via Telegram Bot API.

Configuração por variáveis de ambiente:
    TELEGRAM_BOT_TOKEN   — token retornado pelo @BotFather
    TELEGRAM_CHAT_ID     — id do chat (negativo se for grupo) ou do seu DM com o bot

A mensagem é HTML (parse_mode=HTML) e cada promoção vira um bloco com link
clicável. O Telegram limita 4096 chars/mensagem, então quebramos em chunks.
"""
from __future__ import annotations

import logging
import os
from html import escape
from typing import Iterable, List, Optional

import httpx

from .sizes import format_sizes_compact

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MSG_CHARS = 3800  # margem de segurança vs 4096 do Telegram
TIMEOUT_S = 15.0


class TelegramConfigError(RuntimeError):
    pass


def _fmt_brl(v) -> str:
    if v is None:
        return "—"
    return ("R$ {:,.2f}".format(float(v))
            .replace(",", "X").replace(".", ",").replace("X", "."))


# Emoji por loja — ajuda a varrer visualmente quando vêm várias fontes.
SOURCE_EMOJI = {
    "ous": "🟧",        # OUS oficial
    "netshoes": "🟦",   # Netshoes Clube
    "centauro": "🟥",   # Centauro
}
SOURCE_LABEL = {
    "ous": "OUS oficial",
    "netshoes": "Netshoes Clube",
    "centauro": "Centauro",
}


def _intensity_emoji(pct: int) -> str:
    """Emoji que cresce com a intensidade do desconto."""
    if pct >= 50:
        return "🔥🔥"
    if pct >= 40:
        return "🔥"
    if pct >= 30:
        return "💥"
    if pct >= 20:
        return "🏷️"
    return "💸"


def _format_promo(row) -> str:
    """row: sqlite3.Row com source, sku, name, url, list_price, price,
    prev_price, sizes, stock_qty."""
    list_price = row["list_price"]
    price = row["price"]
    pct = int(round((1 - price / list_price) * 100)) if list_price else 0
    name = escape(row["name"])
    url = escape(row["url"], quote=True)
    source = row["source"]
    src_emoji = SOURCE_EMOJI.get(source, "🛒")
    src_label = SOURCE_LABEL.get(source, source)

    lines = [
        f"{src_emoji} <b><a href=\"{url}\">{name}</a></b>",
        f"   <i>{escape(src_label)}</i>",
    ]

    # Linha de preço com intensidade
    intensity = _intensity_emoji(pct)
    price_line = (
        f"   💰 <b>{_fmt_brl(price)}</b> "
        f"<s>{_fmt_brl(list_price)}</s> "
        f"{intensity} <b>-{pct}%</b>"
    )
    lines.append(price_line)

    # Economia em R$
    if list_price and list_price > price:
        savings = float(list_price) - float(price)
        lines.append(f"   💸 Economiza {_fmt_brl(savings)}")

    # Preço anterior (se for queda em cima de uma promo já existente)
    prev = row["prev_price"]
    if prev is not None and abs(float(prev) - float(price)) > 0.001 and float(prev) != float(list_price or 0):
        prev_brl = _fmt_brl(prev)
        diff = float(prev) - float(price)
        if diff > 0:
            lines.append(f"   📉 Caiu de {prev_brl}")

    # Tamanhos disponíveis
    sizes_csv = row["sizes"]
    sizes_list: list[str] = []
    if sizes_csv:
        sizes_list = [s for s in sizes_csv.split(",") if s.strip()]
    if sizes_list:
        compact = format_sizes_compact(sizes_list)
        lines.append(f"   👟 Tamanhos: {escape(compact)}")

    # Estoque baixo (mesma regra do Product.low_stock)
    stock_qty = row["stock_qty"]
    few_sizes = 0 < len(sizes_list) <= 2
    few_units = stock_qty is not None and 0 < int(stock_qty) <= 3
    if few_sizes or few_units:
        bits = []
        if few_units:
            bits.append(f"{int(stock_qty)} un.")
        if few_sizes:
            bits.append(f"{len(sizes_list)} tam.")
        lines.append(f"   ⚠️ <b>Últimas peças</b> ({', '.join(bits)})")

    return "\n".join(lines)


def _chunk_messages(header: str, lines: List[str]) -> Iterable[str]:
    current = header
    for line in lines:
        candidate = current + "\n\n" + line
        if len(candidate) > MAX_MSG_CHARS:
            yield current
            current = line
        else:
            current = candidate
    if current.strip():
        yield current


def send_promotions(rows, *, bot_token: Optional[str] = None,
                    chat_id: Optional[str] = None,
                    dry_run: bool = False) -> int:
    """Envia uma ou mais mensagens com as promoções novas. Retorna nº de mensagens enviadas."""
    rows = list(rows)
    if not rows:
        log.info("Telegram: nenhuma promoção nova para notificar.")
        return 0

    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not dry_run and (not bot_token or not chat_id):
        raise TelegramConfigError(
            "TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID precisam estar definidos "
            "(ou use dry_run=True). Veja .env.example."
        )

    header = f"<b>🛒 {len(rows)} promoção(ões) ÖUS nova(s)</b>"
    lines = [_format_promo(r) for r in rows]
    messages = list(_chunk_messages(header, lines))

    if dry_run:
        log.info("Telegram (DRY-RUN): enviaria %d mensagem(ns):", len(messages))
        for i, m in enumerate(messages, 1):
            log.info("--- msg %d/%d (%d chars) ---\n%s", i, len(messages), len(m), m)
        return len(messages)

    sent = 0
    url = f"{API_BASE}/bot{bot_token}/sendMessage"
    with httpx.Client(timeout=TIMEOUT_S) as client:
        for msg in messages:
            resp = client.post(url, json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code != 200:
                log.error("Telegram falhou (%s): %s", resp.status_code, resp.text[:300])
                resp.raise_for_status()
            sent += 1
    log.info("Telegram: %d mensagem(ns) enviada(s) com %d promoção(ões).", sent, len(rows))
    return sent
