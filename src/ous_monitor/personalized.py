"""Seleção e envio dos resumos personalizados do bot do Telegram."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .notifier import NOTIFICATION_KEYBOARD, send_alert
from .services import CatalogService, ProductFilters
from .storage import (
    connect,
    list_enabled_alert_preferences,
    list_favorites,
    list_saved_filters,
    record_personalized_alert_delivery,
    was_personalized_alert_delivered,
)

CHANGE_CATEGORIES = ("new_promo", "ended", "weaker", "price_up")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersonalizedDispatchResult:
    chats: int = 0
    events: int = 0


def _value(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def event_key(category: str, row) -> str:
    """Cria uma chave curta e estavel para deduplicar uma mudanca observada."""
    payload = json.dumps(
        [
            category,
            _value(row, "source"),
            _value(row, "sku"),
            _value(row, "observed_at"),
            _value(row, "price"),
            _value(row, "list_price"),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "v1:" + hashlib.sha256(payload).hexdigest()


def select_personalized_changes(
    changes: dict,
    *,
    favorite_keys: Iterable[tuple[str, str]],
    saved_filter_keys: Iterable[tuple[str, str]],
    already_delivered: Callable[[str], bool] | None = None,
) -> tuple[dict, list[str]]:
    """Seleciona eventos acompanhados, sem duplicar filtro e favorito.

    Favoritos acompanham qualquer mudanca. Filtros salvos disparam somente
    para promocao nova/queda de preco que ainda satisfaca o filtro.
    """
    favorites = set(favorite_keys)
    filtered = set(saved_filter_keys)
    selected = {category: [] for category in CHANGE_CATEGORIES}
    keys: list[str] = []
    seen: set[str] = set()

    for category in CHANGE_CATEGORIES:
        for row in changes.get(category, []):
            product_key = (str(_value(row, "source")), str(_value(row, "sku")))
            if product_key not in favorites and not (
                category == "new_promo" and product_key in filtered
            ):
                continue
            key = event_key(category, row)
            if key in seen or (already_delivered and already_delivered(key)):
                continue
            seen.add(key)
            keys.append(key)
            selected[category].append(row)
    return selected, keys


def _saved_filter_product_keys(db_path: Path, rows: Iterable[object]) -> set[tuple[str, str]]:
    catalog = CatalogService(db_path)
    keys: set[tuple[str, str]] = set()
    for row in rows:
        max_price = _value(row, "max_price")
        min_discount = _value(row, "min_discount")
        products = catalog.latest_discounted(
            source=str(_value(row, "source")),
            filters=ProductFilters(
                category=str(_value(row, "category", "all")),
                max_price="all" if max_price is None else str(max_price),
                min_discount="all" if min_discount is None else str(min_discount),
            ),
        )
        keys.update((str(item["source"]), str(item["sku"])) for item in products)
    return keys


def dispatch_personalized_alerts(
    db_path: Path,
    changes: dict,
    *,
    bot_token: str,
    hour_utc: int | None = None,
    sender: Callable = send_alert,
) -> PersonalizedDispatchResult:
    """Envia um resumo por chat e registra os eventos entregues com sucesso."""
    delivered_chats = 0
    delivered_events = 0
    with connect(db_path) as conn:
        preferences = list_enabled_alert_preferences(conn, hour_utc=hour_utc)

    for preference in preferences:
        chat_id = preference["chat_id"]
        try:
            with connect(db_path) as conn:
                saved_rows = list_saved_filters(conn, chat_id)
                favorite_rows = list_favorites(conn, chat_id)
            favorite_keys = {
                (str(row["source"]), str(row["sku"])) for row in favorite_rows
            }
            filter_keys = _saved_filter_product_keys(db_path, saved_rows)

            with connect(db_path) as conn:
                selected, keys = select_personalized_changes(
                    changes,
                    favorite_keys=favorite_keys,
                    saved_filter_keys=filter_keys,
                    already_delivered=lambda key: was_personalized_alert_delivered(
                        conn, chat_id, key
                    ),
                )
            if not keys:
                continue

            sent = sender(
                selected,
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=NOTIFICATION_KEYBOARD,
                summary=True,
                period_label="seus acompanhamentos",
            )
            if not sent:
                continue
            with connect(db_path) as conn:
                for key in keys:
                    if record_personalized_alert_delivery(conn, chat_id, key):
                        delivered_events += 1
            delivered_chats += 1
        except Exception:  # um chat com erro não pode bloquear os seguintes
            log.exception("Falha no alerta personalizado do chat %s", chat_id)

    return PersonalizedDispatchResult(delivered_chats, delivered_events)
