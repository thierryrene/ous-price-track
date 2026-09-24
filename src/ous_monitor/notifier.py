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
import time
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Iterable, List, Optional

import httpx

from .bot.callbacks import encode as callback_data
from .bot.views import build_save_filter_button
from .sizes import format_sizes_compact
from .sources import SOURCES, source_emojis, source_labels

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MSG_CHARS = 3800  # margem de segurança vs 4096 do Telegram
TIMEOUT_S = 15.0
MAX_MESSAGES_PER_BATCH = 2

PENDING_MESSAGES_CACHE: dict[int, dict] = {}

class TelegramConfigError(RuntimeError):
    pass


__all__ = [
    "API_BASE",
    "MAX_MSG_CHARS",
    "MAX_MESSAGES_PER_BATCH",
    "TIMEOUT_S",
    "PENDING_MESSAGES_CACHE",
    "TelegramConfigError",
    "MENU_KEYBOARD",
    "NOTIFICATION_KEYBOARD",
    "STORE_KEYBOARD",
    "UPDATE_KEYBOARD",
    "CATEGORY_KEYBOARD",
    "CATALOG_KEYBOARD",
    "SOURCE_LABEL_SHORT",
    "escape_html",
    "escape_error_html",
    "format_error_message",
    "format_brl",
    "discount_intensity_emoji",
    "chunk_messages",
    "build_summary",
    "send_telegram_batch",
    "send_telegram_messages",
    "build_store_keyboard",
    "build_filter_keyboard",
    "build_filter_message",
    "format_relative_time",
    "build_freshness_message",
    "build_progress_message",
    "send_filter_menu",
    "send_menu_message",
    "send_alert",
    "send_digest",
    "send_promotions",
    "build_update_keyboard",
]


# Filtros (gênero/idade + tênis 42/43) são aplicados na INGESTÃO em
# `cli._scrape_and_persist`, não aqui. O notifier confia que o DB já só contém
# itens elegíveis. Se a vocabulary list dos filtros mudar, rode `purge` pra
# limpar produtos existentes que deixaram de passar.


def escape_html(value, *, quote: bool = False) -> str:
    """Escape text for Telegram HTML messages."""
    return escape("" if value is None else str(value), quote=quote)


def escape_error_html(error) -> str:
    """Escape exception/error text before embedding it in Telegram HTML."""
    return escape_html(error, quote=False)


def format_error_message(title: str, error, *, prefix: str = "❌") -> str:
    """Build a Telegram-safe HTML error message."""
    return f"{prefix} <b>{escape_html(title)}</b>\n<code>{escape_error_html(error)}</code>"


def _fmt_brl(v) -> str:
    if v is None:
        return "—"
    return ("R$ {:,.2f}".format(float(v))
            .replace(",", "X").replace(".", ",").replace("X", "."))


def format_brl(value) -> str:
    """Format a numeric value as BRL, preserving notifier output."""
    return _fmt_brl(value)


# Emoji/label por loja — derivados de sources.py (fonte única; inclui umbro e
# approve). Ajuda a varrer visualmente quando vêm várias fontes.
SOURCE_EMOJI = {**source_emojis()}
SOURCE_LABEL = {**source_labels()}


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


def discount_intensity_emoji(pct: int) -> str:
    """Return the discount intensity emoji used by Telegram notifications."""
    return _intensity_emoji(pct)


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


# ---------------------------------------------------------------------------
# Modo resumo (alta carga): uma linha por item, agrupado por tipo de peça.
# Acionado por volume (SUMMARY_THRESHOLD) e sempre no digest.
# ---------------------------------------------------------------------------

DEFAULT_SUMMARY_THRESHOLD = 15   # nº de mudanças a partir do qual o alert resume
DEFAULT_SUMMARY_PER_GROUP = 12   # máx. de linhas por grupo antes do "…+K mais"


def _summary_threshold() -> int:
    try:
        return int(os.environ.get("SUMMARY_THRESHOLD", DEFAULT_SUMMARY_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_SUMMARY_THRESHOLD


def _summary_per_group() -> int:
    try:
        return int(os.environ.get("SUMMARY_PER_GROUP", DEFAULT_SUMMARY_PER_GROUP))
    except (TypeError, ValueError):
        return DEFAULT_SUMMARY_PER_GROUP


def _disc_pct(row) -> int:
    list_price = row["list_price"]
    price = row["price"]
    if list_price and list_price > price:
        return int(round((1 - price / list_price) * 100))
    return 0


def _brl_compact(value) -> str:
    """BRL sem os centavos quando forem ',00' (densidade no resumo)."""
    s = _fmt_brl(value)
    return s[:-3] if s.endswith(",00") else s


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _format_promo_oneline(row) -> str:
    """Uma linha densa: '🔥 -62% R$ 189 <a>Nome</a> ⟵ R$ 499'."""
    list_price = row["list_price"]
    price = row["price"]
    pct = _disc_pct(row)
    name = escape(_truncate(row["name"], 32))
    url = escape(row["url"], quote=True)
    emoji = _intensity_emoji(pct)
    disc = f"-{pct}%" if pct else "•"
    tail = ""
    if list_price and list_price > price:
        tail = f" ⟵ <s>{_brl_compact(list_price)}</s>"
    return (f"{emoji} <b>{disc}</b> {_brl_compact(price)} "
            f"<a href=\"{url}\">{name}</a>{tail}")


def build_summary(changes: dict, *, period_label: str = "agora") -> List[str]:
    """Resumo de alta carga: agrupa `new_promo` por tipo de peça (uma linha por
    item, ordenado por maior desconto), com cap por grupo. `weaker`/`price_up`
    viram um rodapé compacto de contagem. `ended` é omitido.

    Retorna a lista de mensagens já fatiadas pelo limite do Telegram.
    """
    from .categories import CATEGORY_META, CATEGORY_ORDER, categorize

    new_promo = list(changes.get("new_promo") or [])
    weaker = list(changes.get("weaker") or [])
    price_up = list(changes.get("price_up") or [])

    header = (f"<b>🛒 {len(new_promo)} promoção(ões) nova(s) — "
              f"{escape(period_label)}</b>")

    groups: dict[str, list] = {k: [] for k in CATEGORY_ORDER}
    for row in new_promo:
        groups[categorize(row["name"])].append(row)

    per_group = _summary_per_group()
    lines: List[str] = [""]  # linha em branco após o cabeçalho
    first = True
    for key in CATEGORY_ORDER:
        rows = groups[key]
        if not rows:
            continue
        if not first:
            lines.append("")  # espaço só entre grupos
        first = False
        rows.sort(key=_disc_pct, reverse=True)
        emoji, label = CATEGORY_META[key]
        lines.append(f"<b>{emoji} {escape(label.upper())} ({len(rows)})</b>")
        for row in rows[:per_group]:
            lines.append(_format_promo_oneline(row))
        extra = len(rows) - per_group
        if extra > 0:
            lines.append(f"   …+{extra} mais")

    if weaker or price_up:
        bits = []
        if weaker:
            bits.append(f"📉 {len(weaker)} desconto encolheu")
        if price_up:
            bits.append(f"📈 {len(price_up)} subiu")
        lines.append("")
        lines.append("<b>⚠️ Ficou pior:</b> " + " · ".join(bits))

    return list(_chunk_lines(header, lines))


def _chunk_lines(header: str, lines: List[str]) -> Iterable[str]:
    """Como chunk_messages, mas junta as linhas com '\\n' (uma quebra) em vez de
    '\\n\\n' — o resumo controla o próprio espaçamento via linhas vazias."""
    current = header
    for line in lines:
        candidate = current + "\n" + line
        if len(candidate) > MAX_MSG_CHARS:
            yield current.rstrip()
            current = line
        else:
            current = candidate
    if current.strip():
        yield current.rstrip()


def _chunk_messages(header: str, lines: List[str]) -> Iterable[str]:
    return chunk_messages(header, lines)


def chunk_messages(header: str, lines: List[str]) -> Iterable[str]:
    """Yield Telegram-sized HTML messages from a header plus item blocks."""
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


# Menu principal: consulta e atualização são ações distintas. Consulta usa o
# último catálogo confirmado; atualização pode levar vários minutos.
MENU_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔥 Ver ofertas", "callback_data": callback_data("stores", "menu")},
        ],
        [
            {"text": "🆕 Promoções de hoje", "callback_data": callback_data("run", "daily_promos")},
            {"text": "📈 Maiores descontos", "callback_data": callback_data("catalog", "top_discounts")},
        ],
        [
            {"text": "💾 Filtros salvos", "callback_data": callback_data("saved", "list")},
            {"text": "⭐ Favoritos", "callback_data": callback_data("favorites", "page", 0)},
        ],
        [
            {"text": "🔔 Meus alertas", "callback_data": callback_data("alerts", "prefs")},
        ],
        [
            {"text": "🔄 Atualizar catálogo", "callback_data": callback_data("update", "menu")},
            {"text": "ℹ️ Status", "callback_data": callback_data("catalog", "status")},
        ],
        [
            {"text": "⚙️ Administração", "callback_data": callback_data("catalog", "menu")},
        ]
    ]
}

# Alertas são históricos: abrir o menu a partir deles cria uma nova superfície
# interativa, em vez de substituir o conteúdo da notificação.
NOTIFICATION_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "🏠 Abrir menu", "callback_data": callback_data("home", "new")},
    ]]
}


def build_store_keyboard() -> dict:
    """Build the per-store catalog menu from the canonical source registry."""
    buttons = [
        {
            "text": f"{config.emoji} {config.label}",
            "callback_data": callback_data("run", key),
        }
        for key, config in SOURCES.items()
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([
        {"text": "🔙 Voltar", "callback_data": callback_data("run", "back")},
    ])
    return {"inline_keyboard": rows}


STORE_KEYBOARD = build_store_keyboard()


def build_update_keyboard() -> dict:
    """Build a source menu dedicated to explicit, potentially slow updates."""
    buttons = [
        {
            "text": f"{config.emoji} {config.label}",
            "callback_data": callback_data("update", key),
        }
        for key, config in SOURCES.items()
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([
        {"text": "🔄 Atualizar todas", "callback_data": callback_data("update", "all")},
        {"text": "🔙 Voltar", "callback_data": callback_data("run", "back")},
    ])
    return {"inline_keyboard": rows}


UPDATE_KEYBOARD = build_update_keyboard()


CATALOG_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔄 Forçar Atualização", "callback_data": callback_data("catalog", "force_update")},
            {"text": "📊 Status das Lojas", "callback_data": callback_data("catalog", "status")},
        ],
        [
            {"text": "🗄️ Estatísticas DB", "callback_data": callback_data("catalog", "db_stats")},
            {"text": "🔍 Normalizar Catálogo", "callback_data": callback_data("catalog", "normalize")},
        ],
        [
            {"text": "📈 Top Descontos", "callback_data": callback_data("catalog", "top_discounts")},
            {"text": "🧹 Purgar Produtos", "callback_data": callback_data("catalog", "purge")},
        ],
        [
            {"text": "🔙 Voltar", "callback_data": callback_data("run", "back")}
        ]
    ]
}


def _send_messages(messages, bot_token, chat_id, dry_run, label, reply_markup=None):
    return send_telegram_messages(
        messages,
        bot_token=bot_token,
        chat_id=chat_id,
        dry_run=dry_run,
        label=label,
        reply_markup=reply_markup,
    )


def send_telegram_messages(messages, *, bot_token, chat_id, dry_run=False,
                           label="message", reply_markup=None):
    """Send Telegram HTML messages with batching and continuation support."""
    messages = list(messages)
    if dry_run:
        log.info("Telegram (DRY-RUN, %s): enviaria %d mensagem(ns):", label, len(messages))
        for i, m in enumerate(messages, 1):
            log.info("--- msg %d/%d (%d chars) ---\n%s", i, len(messages), len(m), m)
        return len(messages)

    chat_id_int = int(chat_id)
    total = len(messages)

    if total <= MAX_MESSAGES_PER_BATCH:
        return _send_batch(messages, bot_token, chat_id, reply_markup)

    first_batch = messages[:MAX_MESSAGES_PER_BATCH]
    remaining = messages[MAX_MESSAGES_PER_BATCH:]
    sent = _send_batch(first_batch, bot_token, chat_id, reply_markup=None)

    PENDING_MESSAGES_CACHE[chat_id_int] = {
        "bot_token": bot_token,
        "messages": remaining,
        "reply_markup": reply_markup,
        "label": label,
    }

    summary = (
        f"📋 <b>Resumo do que falta:</b>\n"
        f"Foram enviadas {sent} de {total} mensagens.\n"
        f"Faltam <b>{len(remaining)}</b> mensagem(ns) para serem exibidas."
    )
    continue_keyboard = {
        "inline_keyboard": [
            [
                {"text": "📥 Continuar", "callback_data": callback_data("load", "more")},
                {"text": "❌ Cancelar", "callback_data": callback_data("load", "cancel")},
            ]
        ]
    }
    _send_batch([summary], bot_token, chat_id, continue_keyboard)
    return sent


def _send_batch(messages, bot_token, chat_id, reply_markup=None):
    return send_telegram_batch(
        messages,
        bot_token=bot_token,
        chat_id=chat_id,
        reply_markup=reply_markup,
    )


def send_telegram_batch(messages, *, bot_token, chat_id, reply_markup=None):
    """Send a batch of Telegram HTML messages, adding markup to the last one."""
    messages = list(messages)
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
            if reply_markup and i == len(messages) - 1:
                payload["reply_markup"] = reply_markup

            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                log.error("Telegram falhou (%s): %s", resp.status_code, resp.text[:300])
                resp.raise_for_status()
            sent += 1

            if i < len(messages) - 1:
                time.sleep(1.5)
    return sent


CATEGORY_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "👟 Tênis/Calçados", "callback_data": callback_data("run", "daily_promos", "tenis")},
            {"text": "👕 Roupas em Geral", "callback_data": callback_data("run", "daily_promos", "vestuario")}
        ],
        [
            {"text": "⚽ Camisas de Time", "callback_data": callback_data("run", "daily_promos", "camisas_time")},
            {"text": "🧥 Agasalhos", "callback_data": callback_data("run", "daily_promos", "agasalhos")}
        ],
        [
            {"text": "🧢 Acessórios", "callback_data": callback_data("run", "daily_promos", "acessorios")},
            {"text": "🌟 Todas as Peças", "callback_data": callback_data("run", "daily_promos", "tudo")}
        ],
        [
            {"text": "🔥 Acima de 50% OFF", "callback_data": callback_data("run", "daily_promos", "50off")},
            {"text": "💸 Até R$ 100", "callback_data": callback_data("run", "daily_promos", "ate100")}
        ],
        [
            {"text": "🔙 Voltar", "callback_data": callback_data("run", "back")}
        ]
    ]
}

SOURCE_LABEL_SHORT = {
    key: f"{config.emoji} {config.label}"
    for key, config in SOURCES.items()
}

CATEGORY_OPTIONS = [
    ("tenis", "👟 Tênis"),
    ("vestuario", "👕 Vestuário"),
    ("acessorios", "🧢 Acessórios"),
]

EXTRA_CATEGORIES = [
    ("camisas_time", "⚽ Camisas Time"),
    ("agasalhos", "🧥 Agasalhos"),
]

PRICE_OPTIONS = [
    ("100", "💸 Até R$100"),
    ("200", "💸 Até R$200"),
    ("500", "💸 Até R$500"),
    ("all", "🌟 Sem limite"),
]

DISCOUNT_OPTIONS = [
    ("50", "🔥 50% ou mais"),
    ("30", "💥 30% ou mais"),
    ("all", "🏷️ Todos"),
]


def build_filter_keyboard(source: str, filters: dict, counts: dict | None = None) -> dict:
    """Build the inline keyboard for the filter menu.

    ``filters`` is a dict with keys: category, max_price, min_discount.
    Each value is the option key (e.g. ``"tenis"``, ``"200"``, ``"50"``) or
    ``"all"`` for no filter.
    """
    sel_cat = filters.get("category", "all")
    sel_price = filters.get("max_price", "all")
    sel_disc = filters.get("min_discount", "all")

    def _mark(option_key, current, label):
        return ("✅ " + label) if option_key == current else label

    def _counted(group, option_key, label):
        if not counts:
            return label
        value = counts.get(group, {}).get(option_key)
        return label if value is None else f"{label} · {value}"

    rows = []

    # Category rows (max 3 per line)
    cat_options = CATEGORY_OPTIONS + EXTRA_CATEGORIES

    rows.append([
        {"text": _mark(k, sel_cat, _counted("category", k, label)), "callback_data": callback_data("filter", source, "cat", k)}
        for k, label in cat_options[:3]
    ])
    if len(cat_options) > 3:
        rows.append([
            {"text": _mark(k, sel_cat, _counted("category", k, label)), "callback_data": callback_data("filter", source, "cat", k)}
            for k, label in cat_options[3:6]
        ])
    rows.append([
        {"text": _mark("all", sel_cat, _counted("category", "all", "🌟 Todas")), "callback_data": callback_data("filter", source, "cat", "all")}
    ])

    # Price rows (max 3 per line)
    rows.append([
        {"text": _mark(k, sel_price, _counted("max_price", k, label)), "callback_data": callback_data("filter", source, "price", k)}
        for k, label in PRICE_OPTIONS[:3]
    ])
    rows.append([
        {"text": _mark("all", sel_price, _counted("max_price", "all", "🌟 Sem limite")), "callback_data": callback_data("filter", source, "price", "all")}
    ])

    # Discount row (3 buttons, fits in one line)
    rows.append([
        {"text": _mark(k, sel_disc, _counted("min_discount", k, label)), "callback_data": callback_data("filter", source, "disc", k)}
        for k, label in DISCOUNT_OPTIONS
    ])

    # Consulta rápida pelo último catálogo confirmado; atualização explícita.
    rows.append([
        {"text": "🔥 Ver ofertas", "callback_data": callback_data("filter", source, "run")},
        {"text": "🔄 Atualizar", "callback_data": callback_data("filter", source, "refresh")},
    ])
    rows.append([build_save_filter_button(source)])
    rows.append([
        {"text": "🔙 Lojas", "callback_data": callback_data("stores", "menu")},
    ])

    return {"inline_keyboard": rows}


def build_filter_message(source: str, filters: dict) -> str:
    src_label = SOURCE_LABEL_SHORT.get(source, source)
    parts = [f"<b>{src_label} — Filtros</b>", ""]

    cat = filters.get("category", "all")
    if cat == "all":
        parts.append("📁 Peças: <b>Todas</b>")
    else:
        all_cats = dict(CATEGORY_OPTIONS + EXTRA_CATEGORIES)
        label = all_cats.get(cat, cat)
        parts.append(f"📁 Peças: <b>{label}</b>")

    price = filters.get("max_price", "all")
    if price == "all":
        parts.append("💰 Preço: <b>Sem limite</b>")
    else:
        parts.append(f"💰 Preço: <b>Até R$ {price}</b>")

    disc = filters.get("min_discount", "all")
    if disc == "all":
        parts.append("🏷️ Desconto: <b>Todos</b>")
    else:
        parts.append(f"🏷️ Desconto: <b>{disc}% ou mais</b>")

    parts.append("")
    parts.append(
        "<b>Ver ofertas</b> consulta o último catálogo confirmado. "
        "Use <b>Atualizar</b> para conferir os preços na loja."
    )
    return "\n".join(parts)


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_relative_time(checked_at, *, now=None) -> str:
    """Format a collection timestamp as a short, user-facing relative age."""
    checked = _as_utc(checked_at)
    current = _as_utc(now) or datetime.now(timezone.utc)
    if checked is None:
        return "em horário desconhecido"
    seconds = max(0, int((current - checked).total_seconds()))
    if seconds < 60:
        return "agora"
    minutes = seconds // 60
    if minutes < 60:
        return f"há {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"há {hours}h"
    days = hours // 24
    return f"há {days} dia" if days == 1 else f"há {days} dias"


def _freshness_value(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def build_freshness_message(source, checked_at=None, *, successful=None,
                            fresh_hours=2, expired_hours=8, now=None) -> str:
    """Explain snapshot freshness without implying real-time verification.

    ``source`` may be a source key or a ``SourceFreshness``-like object/dict.
    Keeping this duck-typed avoids coupling the Telegram adapter to services.
    """
    freshness = source if not isinstance(source, str) else None
    if freshness is not None:
        source = _freshness_value(freshness, "source", "")
        checked_at = (_freshness_value(freshness, "last_success_at")
                      or _freshness_value(freshness, "checked_at"))
        if successful is None:
            has_snapshot = _freshness_value(freshness, "has_snapshot")
            if has_snapshot is None:
                has_snapshot = _freshness_value(freshness, "successful_run_id") is not None
            successful = bool(has_snapshot)
        age_seconds = _freshness_value(freshness, "age_seconds")
        latest_status = _freshness_value(freshness, "status")
    else:
        age_seconds = None
        latest_status = None
    if successful is None:
        successful = True
    checked = _as_utc(checked_at)
    current = _as_utc(now) or datetime.now(timezone.utc)
    label = SOURCE_LABEL_SHORT.get(source, source)
    relative = format_relative_time(checked, now=current)
    age_hours = (float(age_seconds) / 3600 if age_seconds is not None
                 else ((current - checked).total_seconds() / 3600 if checked else None))

    if not successful or checked is None:
        return (
            f"🔴 <b>{escape(label)}: catálogo desatualizado</b>\n"
            "A última atualização falhou ou não há coleta confirmada."
        )
    latest_failed = latest_status not in (None, "success")
    failure_note = ("\n⚠️ A tentativa mais recente falhou; estes dados são da "
                    "última coleta completa." if latest_failed else "")
    if age_hours <= fresh_hours:
        return (f"🟢 <b>Preços verificados {relative}</b> · {escape(label)}"
                f"{failure_note}")
    if age_hours <= expired_hours:
        return (
            f"🟡 <b>Preços verificados {relative}</b> · {escape(label)}\n"
            f"Pode haver alterações; você pode atualizar em segundo plano.{failure_note}"
        )
    return (
        f"🔴 <b>Preços verificados {relative}</b> · {escape(label)}\n"
        "Catálogo antigo; atualize antes de confiar nos valores."
    )


def _format_duration(seconds) -> str:
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    minutes, remainder = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m{remainder:02d}s"
    return f"{remainder}s"


def build_progress_message(completed: int, total: int, *, current=None,
                           page=None, total_pages=None, elapsed_seconds=None,
                           eta_seconds=None) -> str:
    """Build the single Telegram message that a background run can edit."""
    lines = [
        "🔄 <b>Atualizando catálogo</b>",
        f"{max(0, completed)} de {max(0, total)} lojas concluídas",
    ]
    if current:
        current_label = SOURCE_LABEL_SHORT.get(current, current)
        detail = f"Agora: {escape(current_label)}"
        if page is not None and total_pages:
            detail += f" — página {page}/{total_pages}"
        lines.append(detail)
    timings = []
    if elapsed_seconds is not None:
        timings.append(f"decorrido: {_format_duration(elapsed_seconds)}")
    if eta_seconds is not None:
        timings.append(f"estimativa: ~{_format_duration(eta_seconds)}")
    if timings:
        lines.append(" · ".join(timings))
    lines.append("Você pode continuar usando o bot.")
    return "\n".join(lines)


def send_filter_menu(bot_token: str, chat_id: str, source: str, filters: dict,
                     edit_message_id: int | None = None) -> int:
    """Send or edit the filter menu message."""
    text = build_filter_message(source, filters)
    keyboard = build_filter_keyboard(source, filters)

    url = f"{API_BASE}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard,
    }

    with httpx.Client(timeout=TIMEOUT_S) as client:
        if edit_message_id:
            edit_url = f"{API_BASE}/bot{bot_token}/editMessageText"
            edit_payload = {
                "chat_id": chat_id,
                "message_id": edit_message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            }
            resp = client.post(edit_url, json=edit_payload)
            if resp.status_code == 200:
                return 1
            try:
                description = str(resp.json().get("description", ""))
            except Exception:
                description = resp.text
            if "message is not modified" in description.lower():
                return 1
            log.warning(
                "Telegram não pôde editar menu %s (%s): %s",
                edit_message_id,
                resp.status_code,
                description[:300],
            )
        resp = client.post(url, json=payload)
        if resp.status_code != 200:
            log.error("Telegram filter menu falhou (%s): %s", resp.status_code, resp.text[:300])
            return 0
    return 1


def send_menu_message(bot_token=None, chat_id=None,
                      text="👟 <b>Ofertas Streetwear</b>\nEscolha uma opção:",
                      dry_run=False) -> int:
    """Envia uma mensagem contendo apenas o teclado de menu do bot."""
    bot_token, chat_id = _resolve_creds(bot_token, chat_id, dry_run)
    return _send_messages([text], bot_token, chat_id, dry_run, "menu", reply_markup=MENU_KEYBOARD)


def _alert_updated_at(changes: dict, updated_at=None) -> datetime:
    value = updated_at
    if value is None:
        observed = []
        for rows in changes.values():
            for row in rows or []:
                try:
                    candidate = row["observed_at"]
                except (KeyError, IndexError, TypeError):
                    candidate = None
                if candidate:
                    observed.append(str(candidate))
        value = max(observed) if observed else datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone(timedelta(hours=-3), name="BRT"))


def _with_update_line(messages: Iterable[str], changes: dict, updated_at=None) -> List[str]:
    local = _alert_updated_at(changes, updated_at)
    line = f"<i>Atualizado em {local.strftime('%d/%m/%Y às %H:%M')} (BRT)</i>"
    return [f"{line}\n{message}" for message in messages]


def send_alert(changes: dict, *, bot_token=None, chat_id=None, dry_run=False,
               reply_markup=NOTIFICATION_KEYBOARD, summary=None, period_label="agora",
               updated_at=None) -> int:
    """Modo alerta-ao-vivo: junta todas as categorias num bloco com cabeçalho.

    `changes` é o dict retornado por storage.find_changes (chaves: new_promo,
    ended, weaker, price_up). Retorna nº de mensagens enviadas (0 se nada).

    Em alta carga vira um **resumo** (uma linha por item, agrupado por tipo de
    peça): `summary=None` decide automaticamente pelo limiar SUMMARY_THRESHOLD;
    `summary=True/False` força.
    """
    counts = {k: len(v) for k, v in changes.items()}
    total = sum(counts.values())
    if total == 0:
        log.info("Telegram: nenhuma mudança para notificar.")
        return 0

    bot_token, chat_id = _resolve_creds(bot_token, chat_id, dry_run)

    use_summary = (total >= _summary_threshold()) if summary is None else summary
    if use_summary:
        messages = _with_update_line(
            build_summary(changes, period_label=period_label), changes, updated_at,
        )
        sent = _send_messages(messages, bot_token, chat_id, dry_run, "alert-resumo",
                              reply_markup=reply_markup)
        log.info("Telegram alert (resumo): %d msg(s) com %d mudança(s) (%s).",
                 sent, total, ", ".join(f"{k}={v}" for k, v in counts.items() if v))
        return sent

    header_bits = []
    if counts.get("new_promo"):
        header_bits.append(f"🆕 {counts['new_promo']} nova(s)")
    if counts.get("ended"):
        header_bits.append(f"🔚 {counts['ended']} acabou")
    if counts.get("weaker"):
        header_bits.append(f"📉 {counts['weaker']} piorou")
    if counts.get("price_up"):
        header_bits.append(f"📈 {counts['price_up']} subiu")
    header = f"<b>🛒 Price Monitor — {' · '.join(header_bits)}</b>"

    # Ordem visual: novidades primeiro, depois sinais negativos.
    lines: List[str] = []
    for cat in ("new_promo", "ended", "weaker", "price_up"):
        for row in changes.get(cat, []):
            lines.append(_format_for_category(cat, row))

    messages = _with_update_line(
        _chunk_messages(header, lines), changes, updated_at,
    )
    sent = _send_messages(messages, bot_token, chat_id, dry_run, "alert", reply_markup=reply_markup)
    log.info("Telegram alert: %d msg(s) com %d mudança(s) (%s).",
             sent, total, ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    return sent


def send_digest(changes: dict, *, period_label: str = "hoje",
                bot_token=None, chat_id=None, dry_run=False,
                reply_markup=NOTIFICATION_KEYBOARD,
                summary=True, updated_at=None) -> int:
    """Modo digest: por default um **resumo** compacto (uma linha por item,
    agrupado por tipo de peça). Passe `summary=False` para o formato antigo de
    4 seções detalhadas."""
    counts = {k: len(v) for k, v in changes.items()}
    total = sum(counts.values())
    if total == 0:
        log.info("Telegram digest: nada a notificar para %s.", period_label)
        return 0

    bot_token, chat_id = _resolve_creds(bot_token, chat_id, dry_run)

    if summary:
        messages = _with_update_line(
            build_summary(changes, period_label=period_label), changes, updated_at,
        )
        sent = _send_messages(messages, bot_token, chat_id, dry_run, "digest-resumo",
                              reply_markup=reply_markup)
        log.info("Telegram digest (resumo): %d msg(s) com %d mudança(s).", sent, total)
        return sent

    sections_order = [
        ("new_promo", "🆕", "Promoções novas"),
        ("weaker", "📉", "Promoções enfraqueceram"),
        ("ended", "🔚", "Promoções terminaram"),
        ("price_up", "📈", "Preços subiram"),
    ]
    header = f"<b>📊 Resumo Price Monitor — {escape(period_label)}</b>"
    lines: List[str] = []
    for cat, emoji, title in sections_order:
        rows = changes.get(cat) or []
        if not rows:
            continue
        # Cabeçalho da seção como uma "linha" gerenciada pelo chunker.
        lines.append(f"<b>{emoji} {escape(title)} ({len(rows)})</b>")
        for row in rows:
            lines.append(_format_for_category(cat, row))

    messages = _with_update_line(
        _chunk_messages(header, lines), changes, updated_at,
    )
    sent = _send_messages(messages, bot_token, chat_id, dry_run, "digest", reply_markup=reply_markup)
    log.info("Telegram digest: %d msg(s) com %d mudança(s).", sent, total)
    return sent


def send_promotions(rows, *, bot_token=None, chat_id=None, dry_run=False) -> int:
    """Wrapper de retrocompatibilidade — mantém a antiga assinatura."""
    return send_alert({"new_promo": list(rows), "ended": [], "weaker": [], "price_up": []},
                      bot_token=bot_token, chat_id=chat_id, dry_run=dry_run)
