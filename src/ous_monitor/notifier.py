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


# Filtros (gênero/idade + tênis 42/43) são aplicados na INGESTÃO em
# `cli._scrape_and_persist`, não aqui. O notifier confia que o DB já só contém
# itens elegíveis. Se a vocabulary list dos filtros mudar, rode `purge` pra
# limpar produtos existentes que deixaram de passar.


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


# Emoji prefixo por categoria de mudança
CATEGORY_PREFIX = {
    "new_promo": "🆕",
    "ended": "🔚",
    "weaker": "📉",
    "price_up": "📈",
}
CATEGORY_TITLE = {
    "new_promo": "Promoção nova",
    "ended": "Acabou a promo",
    "weaker": "Desconto piorou",
    "price_up": "Subiu de preço",
}


def _name_url_source(row):
    return (
        escape(row["name"]),
        escape(row["url"], quote=True),
        row["source"],
        SOURCE_EMOJI.get(row["source"], "🛒"),
        SOURCE_LABEL.get(row["source"], row["source"]),
    )


def _format_sizes_and_stock_lines(row) -> List[str]:
    out: List[str] = []
    sizes_csv = row["sizes"]
    sizes_list: list[str] = []
    if sizes_csv:
        sizes_list = [s for s in sizes_csv.split(",") if s.strip()]
    if sizes_list:
        compact = format_sizes_compact(sizes_list)
        out.append(f"   👟 Tamanhos: {escape(compact)}")
    stock_qty = row["stock_qty"]
    few_sizes = 0 < len(sizes_list) <= 2
    few_units = stock_qty is not None and 0 < int(stock_qty) <= 3
    if few_sizes or few_units:
        bits = []
        if few_units:
            bits.append(f"{int(stock_qty)} un.")
        if few_sizes:
            bits.append(f"{len(sizes_list)} tam.")
        out.append(f"   ⚠️ <b>Últimas peças</b> ({', '.join(bits)})")
    return out


def _format_promo(row) -> str:
    """Formato 'promoção nova' (categoria new_promo). Mantém layout original."""
    list_price = row["list_price"]
    price = row["price"]
    pct = int(round((1 - price / list_price) * 100)) if list_price else 0
    name, url, source, src_emoji, src_label = _name_url_source(row)

    lines = [
        f"{CATEGORY_PREFIX['new_promo']} {src_emoji} <b><a href=\"{url}\">{name}</a></b>",
        f"   <i>{escape(src_label)}</i>",
    ]
    intensity = _intensity_emoji(pct)
    lines.append(
        f"   💰 <b>{_fmt_brl(price)}</b> "
        f"<s>{_fmt_brl(list_price)}</s> "
        f"{intensity} <b>-{pct}%</b>"
    )
    if list_price and list_price > price:
        savings = float(list_price) - float(price)
        lines.append(f"   💸 Economiza {_fmt_brl(savings)}")

    prev = row["prev_price"]
    if (prev is not None and abs(float(prev) - float(price)) > 0.001
            and float(prev) != float(list_price or 0) and float(prev) > float(price)):
        lines.append(f"   📉 Caiu de {_fmt_brl(prev)}")

    lines.extend(_format_sizes_and_stock_lines(row))
    return "\n".join(lines)


def _format_ended(row) -> str:
    """Promo terminou: produto voltou ao preço cheio."""
    list_price = row["list_price"]
    prev_price = row["prev_price"]
    prev_list = row["prev_list_price"]
    name, url, source, src_emoji, src_label = _name_url_source(row)
    lines = [
        f"{CATEGORY_PREFIX['ended']} {src_emoji} <b><a href=\"{url}\">{name}</a></b>",
        f"   <i>{escape(src_label)}</i>",
    ]
    lines.append(f"   💰 Voltou a <b>{_fmt_brl(list_price)}</b>")
    if prev_price is not None and prev_list is not None and prev_list > 0:
        prev_pct = int(round((1 - prev_price / prev_list) * 100))
        lines.append(f"   ⏮ Estava {_fmt_brl(prev_price)} (-{prev_pct}%)")
    return "\n".join(lines)


def _format_weaker(row) -> str:
    """Promo enfraqueceu: ainda tem desconto, mas é menor."""
    list_price = row["list_price"]
    price = row["price"]
    prev_price = row["prev_price"]
    prev_list = row["prev_list_price"]
    cur_pct = int(round((1 - price / list_price) * 100)) if list_price else 0
    prev_pct = (int(round((1 - prev_price / prev_list) * 100))
                if prev_list and prev_price is not None else None)
    name, url, source, src_emoji, src_label = _name_url_source(row)
    lines = [
        f"{CATEGORY_PREFIX['weaker']} {src_emoji} <b><a href=\"{url}\">{name}</a></b>",
        f"   <i>{escape(src_label)}</i>",
    ]
    if prev_pct is not None:
        lines.append(
            f"   📊 Era <b>-{prev_pct}%</b> ({_fmt_brl(prev_price)}), "
            f"agora <b>-{cur_pct}%</b> ({_fmt_brl(price)})"
        )
    else:
        lines.append(f"   📊 Agora <b>-{cur_pct}%</b> ({_fmt_brl(price)})")
    lines.extend(_format_sizes_and_stock_lines(row))
    return "\n".join(lines)


def _format_price_up(row) -> str:
    """Preço subiu (≥5%) sem encerrar promoção."""
    price = row["price"]
    prev_price = row["prev_price"]
    list_price = row["list_price"]
    name, url, source, src_emoji, src_label = _name_url_source(row)
    lines = [
        f"{CATEGORY_PREFIX['price_up']} {src_emoji} <b><a href=\"{url}\">{name}</a></b>",
        f"   <i>{escape(src_label)}</i>",
    ]
    if prev_price is not None:
        diff = float(price) - float(prev_price)
        diff_pct = int(round(diff / float(prev_price) * 100))
        lines.append(
            f"   💰 De <b>{_fmt_brl(prev_price)}</b> "
            f"para <b>{_fmt_brl(price)}</b> (+{diff_pct}%)"
        )
    else:
        lines.append(f"   💰 Agora {_fmt_brl(price)}")
    if list_price and list_price > price:
        cur_pct = int(round((1 - price / list_price) * 100))
        lines.append(f"   🏷 Ainda em promo (-{cur_pct}%)")
    return "\n".join(lines)


def _format_for_category(category: str, row) -> str:
    if category == "new_promo":
        return _format_promo(row)
    if category == "ended":
        return _format_ended(row)
    if category == "weaker":
        return _format_weaker(row)
    if category == "price_up":
        return _format_price_up(row)
    raise ValueError(f"categoria desconhecida: {category}")


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


def _resolve_creds(bot_token, chat_id, dry_run):
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not dry_run and (not bot_token or not chat_id):
        raise TelegramConfigError(
            "TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID precisam estar definidos "
            "(ou use dry_run=True). Veja .env.example."
        )
    return bot_token, chat_id


# Teclado Inline padrão com as ações de monitoramento
MENU_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🟧 OUS", "callback_data": "run:ous"},
            {"text": "🟦 Netshoes", "callback_data": "run:netshoes"},
            {"text": "🟥 Centauro", "callback_data": "run:centauro"},
        ],
        [
            {"text": "🔄 Rodar Todas", "callback_data": "run:all"},
            {"text": "📊 Snapshot Geral", "callback_data": "run:snapshot"},
        ]
    ]
}


def _send_messages(messages, bot_token, chat_id, dry_run, label, reply_markup=None):
    if dry_run:
        log.info("Telegram (DRY-RUN, %s): enviaria %d mensagem(ns):", label, len(messages))
        for i, m in enumerate(messages, 1):
            log.info("--- msg %d/%d (%d chars) ---\n%s", i, len(messages), len(m), m)
        return len(messages)
    sent = 0
    url = f"{API_BASE}/bot{bot_token}/sendMessage"
    with httpx.Client(timeout=TIMEOUT_S) as client:
        for i, msg in enumerate(messages):
            payload = {
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            # Adiciona os botões inline apenas na última mensagem para não duplicar visualmente
            if reply_markup and i == len(messages) - 1:
                payload["reply_markup"] = reply_markup

            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                log.error("Telegram falhou (%s): %s", resp.status_code, resp.text[:300])
                resp.raise_for_status()
            sent += 1
    return sent


def send_menu_message(bot_token=None, chat_id=None, text="Escolha uma opção no menu abaixo para monitoramento on-demand:", dry_run=False) -> int:
    """Envia uma mensagem contendo apenas o teclado de menu do bot."""
    bot_token, chat_id = _resolve_creds(bot_token, chat_id, dry_run)
    return _send_messages([text], bot_token, chat_id, dry_run, "menu", reply_markup=MENU_KEYBOARD)


def send_alert(changes: dict, *, bot_token=None, chat_id=None, dry_run=False, reply_markup=MENU_KEYBOARD) -> int:
    """Modo alerta-ao-vivo: junta todas as categorias num bloco com cabeçalho.

    `changes` é o dict retornado por storage.find_changes (chaves: new_promo,
    ended, weaker, price_up). Retorna nº de mensagens enviadas (0 se nada).
    """
    counts = {k: len(v) for k, v in changes.items()}
    total = sum(counts.values())
    if total == 0:
        log.info("Telegram: nenhuma mudança para notificar.")
        return 0

    bot_token, chat_id = _resolve_creds(bot_token, chat_id, dry_run)

    header_bits = []
    if counts.get("new_promo"):
        header_bits.append(f"🆕 {counts['new_promo']} nova(s)")
    if counts.get("ended"):
        header_bits.append(f"🔚 {counts['ended']} acabou")
    if counts.get("weaker"):
        header_bits.append(f"📉 {counts['weaker']} piorou")
    if counts.get("price_up"):
        header_bits.append(f"📈 {counts['price_up']} subiu")
    header = f"<b>🛒 ÖUS — {' · '.join(header_bits)}</b>"

    # Ordem visual: novidades primeiro, depois sinais negativos.
    lines: List[str] = []
    for cat in ("new_promo", "ended", "weaker", "price_up"):
        for row in changes.get(cat, []):
            lines.append(_format_for_category(cat, row))

    messages = list(_chunk_messages(header, lines))
    sent = _send_messages(messages, bot_token, chat_id, dry_run, "alert", reply_markup=reply_markup)
    log.info("Telegram alert: %d msg(s) com %d mudança(s) (%s).",
             sent, total, ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    return sent


def send_digest(changes: dict, *, period_label: str = "hoje",
                bot_token=None, chat_id=None, dry_run=False, reply_markup=MENU_KEYBOARD) -> int:
    """Modo digest: 4 seções separadas com totais. Pensado para 1×/dia."""
    counts = {k: len(v) for k, v in changes.items()}
    total = sum(counts.values())
    if total == 0:
        log.info("Telegram digest: nada a notificar para %s.", period_label)
        return 0

    bot_token, chat_id = _resolve_creds(bot_token, chat_id, dry_run)

    sections_order = [
        ("new_promo", "🆕", "Promoções novas"),
        ("weaker", "📉", "Promoções enfraqueceram"),
        ("ended", "🔚", "Promoções terminaram"),
        ("price_up", "📈", "Preços subiram"),
    ]
    header = f"<b>📊 Resumo OUS — {escape(period_label)}</b>"
    lines: List[str] = []
    for cat, emoji, title in sections_order:
        rows = changes.get(cat) or []
        if not rows:
            continue
        # Cabeçalho da seção como uma "linha" gerenciada pelo chunker.
        lines.append(f"<b>{emoji} {escape(title)} ({len(rows)})</b>")
        for row in rows:
            lines.append(_format_for_category(cat, row))

    messages = list(_chunk_messages(header, lines))
    sent = _send_messages(messages, bot_token, chat_id, dry_run, "digest", reply_markup=reply_markup)
    log.info("Telegram digest: %d msg(s) com %d mudança(s).", sent, total)
    return sent


def send_promotions(rows, *, bot_token=None, chat_id=None, dry_run=False) -> int:
    """Wrapper de retrocompatibilidade — mantém a antiga assinatura."""
    return send_alert({"new_promo": list(rows), "ended": [], "weaker": [], "price_up": []},
                      bot_token=bot_token, chat_id=chat_id, dry_run=dry_run)
