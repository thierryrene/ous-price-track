"""Pure builders for the interactive Telegram bot screens.

Every screen builder in this module returns a ``(text, reply_markup)`` tuple.
The text is ready to be sent with Telegram's HTML parse mode and the markup is
an inline keyboard.  Builders do not perform I/O or mutate their inputs, which
makes the rendering contract easy to exercise without a Telegram connection.
"""
from __future__ import annotations

import json
import math
from html import escape
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .callbacks import encode

Button = Dict[str, str]
ReplyMarkup = Dict[str, List[List[Button]]]
View = Tuple[str, ReplyMarkup]

DEFAULT_FAVORITES_PAGE_SIZE = 5
COMMON_ALERT_HOURS_UTC = (8, 12, 18, 21)


def _value(row: object, key: str, default: Any = None) -> Any:
    """Read a value from a dict, ``sqlite3.Row`` or row-like object."""
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _callback(operation: str, *args: object) -> str:
    """Build callback data through the canonical size-validating codec."""
    return encode(operation, *args)


def _back_button() -> Button:
    return {"text": "← Voltar ao início", "callback_data": _callback("run", "back")}


def _parse_criteria(row: object) -> Mapping[str, Any]:
    value = _value(row, "criteria", _value(row, "filters", {}))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {"filtros": value}
    if isinstance(value, Mapping):
        return value
    return {}


def _criteria_summary(criteria: Mapping[str, Any]) -> str:
    labels = {
        "category": "Categoria",
        "max_price": "Preço máximo",
        "min_discount": "Desconto mínimo",
    }
    rendered: List[str] = []
    category_labels = {
        "tenis": "Tênis", "vestuario": "Vestuário",
        "acessorios": "Acessórios", "camisas_time": "Camisas de time",
        "agasalhos": "Agasalhos",
    }
    for key, value in criteria.items():
        if value in (None, "", "all"):
            continue
        label = labels.get(str(key), str(key).replace("_", " ").capitalize())
        if key == "category":
            value = category_labels.get(str(value), value)
        elif key == "max_price":
            value = "R$ " + str(value)
        elif key == "min_discount":
            value = str(value) + "%"
        rendered.append(f"{label}: {value}")
    return " · ".join(rendered) if rendered else "Sem restrições"


def _format_brl(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    formatted = f"{number:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {formatted}"


def build_save_filter_button(source: object) -> Button:
    """Return the reusable button that saves the current filter selection."""
    return {
        "text": "💾 Salvar filtro",
        "callback_data": _callback("saved", "create", source),
    }


def build_favorite_button(ref: object, selected: bool = False) -> Button:
    """Return a favorite toggle suitable for offer/result cards."""
    return {
        "text": "★ Favorito" if selected else "☆ Favoritar",
        "callback_data": _callback("favorite", "toggle", ref),
    }


def build_saved_filters_view(rows: Iterable[object]) -> View:
    """Render saved searches with load/delete controls for each search."""
    saved = list(rows)
    lines = ["<b>💾 Filtros salvos</b>", ""]
    keyboard: List[List[Button]] = []

    if not saved:
        lines.extend(
            [
                "Você ainda não salvou nenhum filtro.",
                "Monte uma busca por loja, categoria, preço ou desconto e toque em <b>Salvar filtro</b>.",
            ]
        )
    else:
        lines.append("Abra uma busca salva ou remova as que não usa mais.")
        for position, row in enumerate(saved, start=1):
            row_id = _value(row, "id")
            if row_id is None:
                raise ValueError("filtro salvo sem id")
            name = escape(str(_value(row, "name", f"Filtro {position}")))
            source = escape(str(_value(row, "source", "Todas as lojas")))
            summary = escape(_criteria_summary(_parse_criteria(row)))
            lines.extend(
                [
                    "",
                    f"<b>{position}. {name}</b>",
                    f"🏪 {source}",
                    f"🎯 {summary}",
                ]
            )
            keyboard.append(
                [
                    {
                        "text": f"🔎 Abrir {position}",
                        "callback_data": _callback("saved", "load", row_id),
                    },
                    {
                        "text": f"🗑 Remover {position}",
                        "callback_data": _callback("saved", "delete", row_id),
                    },
                ]
            )

    keyboard.append([_back_button()])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def build_favorites_view(
    rows: Iterable[object],
    page: int = 0,
    page_size: int = DEFAULT_FAVORITES_PAGE_SIZE,
) -> View:
    """Render one zero-based page of favorites and its navigation controls."""
    if page_size <= 0:
        raise ValueError("page_size deve ser maior que zero")
    favorites = list(rows)
    total_pages = max(1, math.ceil(len(favorites) / page_size))
    current_page = min(max(int(page), 0), total_pages - 1)
    start = current_page * page_size
    visible = favorites[start : start + page_size]

    lines = ["<b>⭐ Favoritos</b>", ""]
    keyboard: List[List[Button]] = []
    if not favorites:
        lines.extend(
            [
                "Sua lista de favoritos está vazia.",
                "Ao consultar ofertas, toque em <b>☆ Favoritar</b> para acompanhar um produto por aqui.",
            ]
        )
    else:
        lines.append(
            f"{len(favorites)} produto{'s' if len(favorites) != 1 else ''} · "
            f"página {current_page + 1}/{total_pages}"
        )
        for offset, row in enumerate(visible, start=1):
            absolute_position = start + offset
            ref = _value(row, "ref")
            if ref is None:
                raise ValueError("favorito sem ref")
            name = escape(str(_value(row, "name", "Produto sem nome")))
            source = escape(str(_value(row, "source", "Loja não informada")))
            url = str(_value(row, "url", "") or "")
            price = _format_brl(_value(row, "price"))
            list_price = _format_brl(_value(row, "list_price"))
            price_line = f"💰 <b>{price}</b>"
            try:
                show_list_price = float(_value(row, "list_price")) > float(_value(row, "price"))
            except (TypeError, ValueError):
                show_list_price = False
            if show_list_price:
                price_line += f"  <s>{list_price}</s>"

            lines.extend(
                [
                    "",
                    f"<b>{absolute_position}. {name}</b>",
                    f"🏪 {source}",
                    price_line,
                ]
            )
            controls: List[Button] = []
            if url:
                controls.append({"text": "🔗 Abrir oferta", "url": url})
            controls.append(
                {
                    "text": "🗑 Desfavoritar",
                    "callback_data": _callback("favorite", "delete", ref),
                }
            )
            keyboard.append(controls)

        if total_pages > 1:
            pagination: List[Button] = []
            if current_page > 0:
                pagination.append(
                    {
                        "text": "‹ Anterior",
                        "callback_data": _callback("favorites", "page", current_page - 1),
                    }
                )
            pagination.append(
                {
                    "text": f"{current_page + 1}/{total_pages}",
                    "callback_data": _callback("favorites", "page", current_page),
                }
            )
            if current_page < total_pages - 1:
                pagination.append(
                    {
                        "text": "Próxima ›",
                        "callback_data": _callback("favorites", "page", current_page + 1),
                    }
                )
            keyboard.append(pagination)

    keyboard.append([_back_button()])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def build_alert_preferences_view(enabled: bool, hour_utc: int) -> View:
    """Render alert enablement and the preferred daily UTC delivery hour."""
    hour = int(hour_utc)
    if not 0 <= hour <= 23:
        raise ValueError("hour_utc deve estar entre 0 e 23")

    status = "Ativados" if enabled else "Pausados"
    lines = [
        "<b>🔔 Preferências de alertas</b>",
        "",
        f"Status: <b>{status}</b>",
        f"Horário do resumo: <b>{hour:02d}:00 UTC</b>",
        f"No horário de Brasília: <b>{(hour - 3) % 24:02d}:00 BRT</b>",
        "",
        "Escolha quando deseja receber o resumo diário dos produtos acompanhados.",
    ]
    toggle_text = "⏸ Pausar alertas" if enabled else "▶ Ativar alertas"
    keyboard: List[List[Button]] = [
        [{"text": toggle_text, "callback_data": _callback("alerts", "toggle")}]
    ]

    choices: Sequence[int] = COMMON_ALERT_HOURS_UTC
    if hour not in choices:
        choices = tuple(sorted((*choices, hour)))
    hour_buttons = [
        {
            "text": ("✅ " if choice == hour else "") + f"{choice:02d}:00",
            "callback_data": _callback("alerts", "hour", choice),
        }
        for choice in choices
    ]
    keyboard.extend(hour_buttons[index : index + 4] for index in range(0, len(hour_buttons), 4))
    keyboard.append([_back_button()])
    return "\n".join(lines), {"inline_keyboard": keyboard}


__all__ = [
    "COMMON_ALERT_HOURS_UTC",
    "DEFAULT_FAVORITES_PAGE_SIZE",
    "View",
    "build_alert_preferences_view",
    "build_favorite_button",
    "build_favorites_view",
    "build_save_filter_button",
    "build_saved_filters_view",
]
