from __future__ import annotations

import asyncio
import html
import logging
import os
import httpx
import threading
import time
from contextlib import asynccontextmanager, suppress
from functools import partial
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from datetime import datetime, timezone, timedelta
from .bot.callbacks import decode_to_legacy, encode as callback_data
from .bot.views import (
    build_alert_preferences_view,
    build_favorite_button,
    build_favorites_view,
    build_saved_filters_view,
)
from .cli import DEFAULT_DB
from .categories import categorize
from .notifier import (
    send_menu_message, API_BASE, MENU_KEYBOARD, NOTIFICATION_KEYBOARD,
    STORE_KEYBOARD, CATEGORY_KEYBOARD,
    CATALOG_KEYBOARD, send_digest, SOURCE_LABEL_SHORT,
    PENDING_MESSAGES_CACHE, send_telegram_batch, MAX_MESSAGES_PER_BATCH,
    send_telegram_messages, format_error_message, format_brl, discount_intensity_emoji,
    UPDATE_KEYBOARD, build_freshness_message, build_progress_message, build_summary,
    build_filter_keyboard, build_filter_message, format_relative_time,
)
from .sources import SOURCES, source_keys
from urllib.parse import urlparse
from .services import CatalogService, MonitorService, ProductFilters, run_exclusive
from .storage import (
    connect,
    delete_favorite,
    delete_saved_filter,
    find_changes,
    get_scheduler_slot,
    get_alert_preferences,
    get_or_create_product_ref,
    latest_source_runs,
    list_favorites,
    list_saved_filters,
    load_saved_filter,
    load_bot_session,
    resolve_product_ref,
    save_bot_session,
    save_filter,
    set_alert_preferences,
    set_scheduler_slot,
    toggle_favorite,
)
from .personalized import PersonalizedDispatchResult, dispatch_personalized_alerts

log = logging.getLogger("ous_monitor.server")

_active_updates: set[str] = set()
_active_updates_lock = threading.Lock()
CATALOG_PAGE_SIZE = 5
HOME_TEXT = "👟 <b>Ofertas Streetwear</b>\nEscolha uma opção:"

# Carrega e inicializa o logger básico caso não tenha sido inicializado
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# URLs da API do Telegram contêm o token; nunca permita que httpx/httpcore as
# escrevam em logs INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "sim"}


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
        return value if value > 0 else default
    except (TypeError, ValueError):
        log.warning("%s inválido; usando %d", name, default)
        return default


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = []
    _app.state.telegram_client = httpx.AsyncClient(timeout=10.0)
    _app.state.telegram_sync_client = httpx.Client(timeout=10.0)
    if _env_bool("AUTO_MAINTENANCE_ENABLED", True):
        tasks.append(asyncio.create_task(_maintenance_loop()))
    if _env_bool("PERSONALIZED_ALERTS_ENABLED", True):
        tasks.append(asyncio.create_task(_personalized_alert_loop()))
    if _env_bool("AUTO_MONITOR_ENABLED", True):
        tasks.append(asyncio.create_task(_scheduled_monitor_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await _app.state.telegram_client.aclose()
        _app.state.telegram_sync_client.close()


app = FastAPI(title="OUS Price Monitor Webhook Bot Server", lifespan=lifespan)


def _env_csv_ints(name: str) -> set[int]:
    raw = os.environ.get(name, "")
    out: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.add(int(item))
        except ValueError:
            log.warning("%s contém valor inválido: %s", name, item)
    return out


def _is_allowed_chat(chat_id: int | str) -> bool:
    allowed = _env_csv_ints("TELEGRAM_ALLOWED_CHAT_IDS")
    if not allowed:
        allowed = _env_csv_ints("TELEGRAM_CHAT_ID")
    try:
        return bool(allowed) and int(chat_id) in allowed
    except (TypeError, ValueError):
        return False


def _check_webhook_secret(request: Request) -> bool:
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not expected:
        return True
    return request.headers.get("X-Telegram-Bot-Api-Secret-Token") == expected


async def _send_text(bot_token: str, chat_id: str | int, text: str, *,
                     reply_markup=None, disable_web_page_preview: bool = False):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if disable_web_page_preview:
        payload["disable_web_page_preview"] = True
    return await _telegram_post(bot_token, "sendMessage", payload)


def _telegram_error_description(response) -> str:
    try:
        body = response.json()
        return str(body.get("description", "")) if isinstance(body, dict) else ""
    except Exception:
        return str(getattr(response, "text", ""))


def _telegram_edit_succeeded(response) -> bool:
    if getattr(response, "status_code", 0) == 200:
        return True
    return "message is not modified" in _telegram_error_description(response).lower()


async def _edit_or_send_text(
    bot_token: str,
    chat_id: str | int,
    message_id: int | None,
    text: str,
    *,
    reply_markup=None,
    disable_web_page_preview: bool = False,
):
    """Edita a tela de um callback; envia outra apenas se ela não for editável."""
    if message_id:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
        }
        if disable_web_page_preview:
            payload["disable_web_page_preview"] = True
        response = await _telegram_post(bot_token, "editMessageText", payload)
        if _telegram_edit_succeeded(response):
            return response
        log.warning(
            "Não foi possível editar mensagem %s: %s",
            message_id,
            _telegram_error_description(response),
        )
    return await _send_text(
        bot_token,
        chat_id,
        text,
        reply_markup=reply_markup,
        disable_web_page_preview=disable_web_page_preview,
    )


async def _telegram_post(bot_token: str, method: str, payload: dict,
                         timeout: float = 10.0):
    """Use the lifespan client so webhook handling does not recreate TLS clients."""
    client = getattr(app.state, "telegram_client", None)
    if client is not None:
        return await client.post(
            f"{API_BASE}/bot{bot_token}/{method}", json=payload, timeout=timeout
        )
    # Unit tests may invoke the endpoint without running the ASGI lifespan.
    async with httpx.AsyncClient(timeout=timeout) as temporary:
        return await temporary.post(
            f"{API_BASE}/bot{bot_token}/{method}", json=payload
        )


async def _run_sync(func, *args, **kwargs):
    """Run blocking adapters off the event loop (compatible with Python 3.8)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


def _reserve_update(source: str) -> bool:
    with _active_updates_lock:
        # MonitorService currently uses one process-wide file lock. Reflect that
        # exclusivity before queuing work so a second click gets useful feedback.
        if _active_updates:
            return False
        _active_updates.add(source)
        return True


def _release_update(source: str) -> None:
    with _active_updates_lock:
        _active_updates.discard(source)


def _default_filter_state(source: str) -> dict:
    return {
        "source": source,
        "category": "all",
        "max_price": "all",
        "min_discount": "all",
    }


def _load_filter_state(chat_id: int | str, source: str) -> dict:
    with connect(DEFAULT_DB) as conn:
        state = load_bot_session(conn, chat_id)
        if not state or state.get("source") != source:
            state = _default_filter_state(source)
            save_bot_session(conn, chat_id, state)
    state.pop("ui_message_id", None)
    return state


def _save_filter_state(
    chat_id: int | str,
    state: dict,
    *,
    message_id: int | None = None,
) -> None:
    with connect(DEFAULT_DB) as conn:
        save_bot_session(conn, chat_id, state, ui_message_id=message_id)


def _filter_view(source: str, state: dict) -> tuple[str, dict]:
    counts = CatalogService(DEFAULT_DB).filter_option_counts(source)
    return (
        build_filter_message(source, state),
        build_filter_keyboard(source, state, counts=counts),
    )


def _saved_filter_name(source: str, state: dict) -> str:
    bits = [SOURCES[source].label]
    category = state.get("category", "all")
    if category != "all":
        labels = {
            "tenis": "Tênis", "vestuario": "Vestuário",
            "acessorios": "Acessórios", "camisas_time": "Camisas time",
            "agasalhos": "Agasalhos",
        }
        bits.append(labels.get(category, str(category)))
    if state.get("max_price", "all") != "all":
        bits.append(f"até R${state['max_price']}")
    if state.get("min_discount", "all") != "all":
        bits.append(f"{state['min_discount']}%+")
    return " · ".join(bits)[:60]


def _saved_filter_rows_for_view(rows) -> list[dict]:
    return [
        {
            **dict(row),
            "source": SOURCE_LABEL_SHORT.get(row["source"], row["source"]),
            "criteria": {
                "category": row["category"],
                "max_price": _number_option(row["max_price"]),
                "min_discount": _number_option(row["min_discount"]),
            },
        }
        for row in rows
    ]


def _favorites_view(chat_id: int | str, page: int) -> tuple[str, dict]:
    with connect(DEFAULT_DB) as conn:
        rows = list_favorites(conn, chat_id)
        state = load_bot_session(conn, chat_id) or {}
        state.update({"screen": "favorites", "page": max(0, int(page))})
        save_bot_session(
            conn, chat_id, state,
        )
    view_rows = [
        {**dict(row), "source": SOURCE_LABEL_SHORT.get(row["source"], row["source"])}
        for row in rows
    ]
    return build_favorites_view(view_rows, page=page, page_size=CATALOG_PAGE_SIZE)


def _saved_filters_view(chat_id: int | str, note: str | None = None) -> tuple[str, dict]:
    with connect(DEFAULT_DB) as conn:
        rows = list_saved_filters(conn, chat_id)
        state = load_bot_session(conn, chat_id) or {}
        state["screen"] = "saved"
        save_bot_session(conn, chat_id, state)
    text, markup = build_saved_filters_view(_saved_filter_rows_for_view(rows))
    if note:
        text = f"{text}\n\n{note}"
    return text, markup


def _create_saved_filter(chat_id: int | str, source: str, state: dict):
    with connect(DEFAULT_DB) as conn:
        return save_filter(
            conn,
            chat_id,
            _saved_filter_name(source, state),
            source=source,
            category=state.get("category", "all"),
            max_price=state.get("max_price", "all"),
            min_discount=state.get("min_discount", "all"),
        )


def _number_option(value) -> str:
    if value is None:
        return "all"
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def _load_saved_filter_state(chat_id: int | str, filter_id: int) -> dict | None:
    with connect(DEFAULT_DB) as conn:
        row = load_saved_filter(conn, chat_id, filter_id)
        if row is None:
            return None
        state = {
            "source": row["source"],
            "category": row["category"],
            "max_price": _number_option(row["max_price"]),
            "min_discount": _number_option(row["min_discount"]),
            "screen": "offers",
            "offset": 0,
        }
        save_bot_session(conn, chat_id, state)
        return state


def _remove_saved_filter(chat_id: int | str, filter_id: int) -> bool:
    with connect(DEFAULT_DB) as conn:
        return delete_saved_filter(conn, chat_id, filter_id)


def _change_favorite(chat_id: int | str, ref: str, *, remove: bool = False):
    with connect(DEFAULT_DB) as conn:
        try:
            product = resolve_product_ref(conn, ref)
        except ValueError:
            return None
        if product is None:
            return None
        if remove:
            return delete_favorite(conn, chat_id, product["source"], product["sku"])
        return toggle_favorite(conn, chat_id, product["source"], product["sku"])


def _load_bot_state(chat_id: int | str) -> dict:
    with connect(DEFAULT_DB) as conn:
        return load_bot_session(conn, chat_id) or {}


def _alert_preferences_view(
    chat_id: int | str, *, enabled: bool | None = None, hour_utc: int | None = None
) -> tuple[str, dict]:
    with connect(DEFAULT_DB) as conn:
        current = get_alert_preferences(conn, chat_id)
        if enabled is not None or hour_utc is not None:
            current = set_alert_preferences(
                conn,
                chat_id,
                enabled=current["enabled"] if enabled is None else enabled,
                hour_utc=current["hour_utc"] if hour_utc is None else hour_utc,
            )
        state = load_bot_session(conn, chat_id) or {}
        state["screen"] = "alerts"
        save_bot_session(conn, chat_id, state)
    return build_alert_preferences_view(current["enabled"], current["hour_utc"])


def _get_alert_preference_state(chat_id: int | str) -> dict:
    with connect(DEFAULT_DB) as conn:
        return get_alert_preferences(conn, chat_id)


def _sync_telegram_post(bot_token: str, method: str, payload: dict):
    client = getattr(app.state, "telegram_sync_client", None)
    post = client.post if client is not None else httpx.post
    return post(
        f"{API_BASE}/bot{bot_token}/{method}", json=payload, timeout=10.0
    )


def _render_text_sync(
    bot_token: str,
    chat_id: str | int,
    message_id: int | None,
    text: str,
    *,
    reply_markup=None,
    disable_web_page_preview: bool = False,
) -> None:
    """Versão síncrona para tarefas FastAPI executadas em worker thread."""
    common = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    }
    if disable_web_page_preview:
        common["disable_web_page_preview"] = True
    if message_id:
        response = _sync_telegram_post(
            bot_token,
            "editMessageText",
            {**common, "message_id": message_id},
        )
        if _telegram_edit_succeeded(response):
            return
        log.warning(
            "Não foi possível editar mensagem %s: %s",
            message_id,
            _telegram_error_description(response),
        )
    response = _sync_telegram_post(bot_token, "sendMessage", common)
    if getattr(response, "status_code", 0) != 200:
        log.error(
            "Telegram falhou ao renderizar tela (%s): %s",
            getattr(response, "status_code", "?"),
            _telegram_error_description(response),
        )


def _edit_progress(bot_token: str, chat_id: str | int, message_id: int | None,
                   text: str, reply_markup=None) -> None:
    if not message_id:
        return
    try:
        response = _sync_telegram_post(
            bot_token,
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
            },
        )
        if not _telegram_edit_succeeded(response):
            log.warning(
                "Falha ao atualizar progresso %s: %s",
                message_id,
                _telegram_error_description(response),
            )
    except Exception:
        log.exception("Falha ao atualizar mensagem de progresso")


def _progress_editor(bot_token: str, chat_id: str | int,
                     message_id: int | None, started: float):
    if not message_id:
        return None
    active: list[str] = []

    def update(event) -> None:
        if event.event == "start" and event.source not in active:
            active.append(event.source)
        elif event.event == "finish" and event.source in active:
            active.remove(event.source)
        current = active[0] if active else None
        _edit_progress(
            bot_token,
            chat_id,
            message_id,
            build_progress_message(
                event.completed,
                event.total,
                current=current,
                elapsed_seconds=time.monotonic() - started,
            ),
        )

    return update


def run_automatic_maintenance() -> bool:
    interval_hours = _env_positive_int("MAINTENANCE_INTERVAL_HOURS", 24)
    backup_dir = DEFAULT_DB.parent / "backups"
    backups = list(backup_dir.glob("prices-*.db")) if backup_dir.exists() else []
    if backups:
        newest = max(backups, key=lambda path: path.stat().st_mtime)
        age_seconds = datetime.now(timezone.utc).timestamp() - newest.stat().st_mtime
        if age_seconds < interval_hours * 3600:
            log.info("Manutenção automática ainda não venceu; execução ignorada")
            return False

    result = CatalogService(DEFAULT_DB).maintain(
        retention_days=_env_positive_int("MAINTENANCE_RETENTION_DAYS", 90),
        run_retention_days=_env_positive_int("MAINTENANCE_RUN_RETENTION_DAYS", 180),
        max_db_mb=_env_positive_int("MAINTENANCE_MAX_DB_MB", 50),
        backup_keep=_env_positive_int("MAINTENANCE_BACKUP_KEEP", 7),
    )
    log.info(
        "Manutenção automática concluída: observações=%d produtos_inválidos=%d "
        "runs=%d tamanho=%d->%d backup=%s",
        result.removed_observations,
        result.removed_bad_products,
        result.removed_runs,
        result.before_bytes,
        result.after_bytes,
        result.backup_path.name,
    )
    if result.within_size_limit:
        return True

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        send_telegram_messages(
            [
                "⚠️ <b>Alerta de manutenção do Price Monitor</b>\n\n"
                f"O banco permanece com {result.after_bytes / (1024 * 1024):.1f} MB "
                f"após a compactação; limite configurado: "
                f"{result.max_bytes / (1024 * 1024):.0f} MB."
            ],
            bot_token=bot_token,
            chat_id=chat_id,
            label="maintenance_size_warning",
            reply_markup=NOTIFICATION_KEYBOARD,
        )
    return True


async def _maintenance_loop() -> None:
    initial_delay = _env_positive_int("MAINTENANCE_INITIAL_DELAY_SECONDS", 300)
    interval_hours = _env_positive_int("MAINTENANCE_INTERVAL_HOURS", 24)
    await asyncio.sleep(initial_delay)
    while True:
        ran = False
        try:
            loop = asyncio.get_running_loop()
            ran = await loop.run_in_executor(None, run_automatic_maintenance)
        except Exception:
            log.exception("Falha na manutenção automática do banco")
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if bot_token and chat_id:
                try:
                    await loop.run_in_executor(
                        None,
                        partial(
                            send_telegram_messages,
                            [
                                "❌ <b>Falha na manutenção automática do banco.</b>\n"
                                "O monitor continuará funcionando, mas o banco precisa "
                                "ser verificado."
                            ],
                            bot_token=bot_token,
                            chat_id=chat_id,
                            label="maintenance_failure",
                            reply_markup=NOTIFICATION_KEYBOARD,
                        ),
                    )
                except Exception:
                    log.exception("Falha ao enviar alerta de manutenção")
        # Se um restart ocorreu logo após uma manutenção, confira novamente em
        # uma hora sem criar backups duplicados; após executar, aguarde o ciclo.
        await asyncio.sleep(interval_hours * 3600 if ran else 3600)


def run_personalized_alert_dispatch(now: datetime | None = None):
    """Entrega os acompanhamentos que vencem na hora UTC atual."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    window_hours = _env_positive_int("PERSONALIZED_ALERT_WINDOW_HOURS", 26)
    since = (current - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    with connect(DEFAULT_DB) as conn:
        changes = find_changes(conn, since)
    # Inclua também a hora anterior: uma coleta concluída nos últimos minutos
    # da hora não deve esperar até o dia seguinte para entrar no resumo.
    due_hours = {current.hour, (current - timedelta(hours=1)).hour}
    results = [
        dispatch_personalized_alerts(
            DEFAULT_DB, changes, bot_token=bot_token, hour_utc=hour,
        )
        for hour in due_hours
    ]
    result = PersonalizedDispatchResult(
        chats=sum(item.chats for item in results),
        events=sum(item.events for item in results),
    )
    if result.events:
        log.info(
            "Alertas personalizados: %d evento(s) para %d chat(s)",
            result.events,
            result.chats,
        )
    return result


async def _personalized_alert_loop() -> None:
    initial_delay = _env_positive_int("PERSONALIZED_ALERT_INITIAL_DELAY_SECONDS", 60)
    interval_seconds = _env_positive_int("PERSONALIZED_ALERT_INTERVAL_SECONDS", 900)
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await _run_sync(run_personalized_alert_dispatch)
        except Exception:
            log.exception("Falha ao entregar alertas personalizados")
        await asyncio.sleep(interval_seconds)


def _monitor_schedule() -> list[tuple[int, str]]:
    """Parse the UTC schedule used by the in-process catalog monitor."""
    raw = os.environ.get("MONITOR_SCHEDULE_UTC", "12:alert,21:digest")
    schedule: dict[int, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            hour_text, mode = item.split(":", 1)
            hour = int(hour_text)
        except (TypeError, ValueError):
            log.warning("Horário inválido em MONITOR_SCHEDULE_UTC: %s", item)
            continue
        mode = mode.strip().lower()
        if not 0 <= hour <= 23 or mode not in {"alert", "digest"}:
            log.warning("Horário/modo inválido em MONITOR_SCHEDULE_UTC: %s", item)
            continue
        schedule[hour] = mode
    if not schedule:
        log.error("MONITOR_SCHEDULE_UTC sem horários válidos; usando 12:alert,21:digest")
        return [(12, "alert"), (21, "digest")]
    return sorted(schedule.items())


def _latest_due_monitor_slot(
    now: datetime | None = None,
) -> tuple[str, str]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    candidates: list[tuple[datetime, str]] = []
    for day_offset in (0, -1):
        day = (current + timedelta(days=day_offset)).date()
        for hour, mode in _monitor_schedule():
            due_at = datetime(
                day.year, day.month, day.day, hour, tzinfo=timezone.utc,
            )
            if due_at <= current:
                candidates.append((due_at, mode))
    due_at, mode = max(candidates, key=lambda item: item[0])
    return due_at.strftime("%Y-%m-%dT%H:00Z"), mode


def run_scheduled_monitor(slot: str, mode: str) -> bool:
    """Refresh every scheduled source once for a persistent UTC slot."""
    with connect(DEFAULT_DB) as conn:
        completed_slot = get_scheduler_slot(conn, "catalog_monitor")
        # A manual catch-up may finish after its nominal UTC slot. Treat that
        # later slot as covering earlier due slots from the same day.
        if completed_slot is not None and completed_slot >= slot:
            return False

    sources = source_keys()
    result = run_exclusive(
        lambda: MonitorService(DEFAULT_DB).run(
            sources=sources,
            mode=mode,
            digest_hours=24,
        )
    )
    if not result.scrape.products:
        raise RuntimeError(
            "A atualização agendada não coletou produtos: "
            + ", ".join(result.scrape.failed)
        )

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        if result.total_changes:
            if mode == "digest":
                send_digest(
                    result.changes,
                    period_label="últimas 24h",
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=NOTIFICATION_KEYBOARD,
                    updated_at=datetime.now(timezone.utc),
                )
            else:
                from .notifier import send_alert
                send_alert(
                    result.changes,
                    period_label=f"coleta {slot}",
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=NOTIFICATION_KEYBOARD,
                    updated_at=datetime.now(timezone.utc),
                )
        else:
            send_telegram_messages(
                [
                    "✅ <b>Catálogo atualizado.</b>\n"
                    f"Coleta {slot}: nenhuma mudança de preço encontrada."
                ],
                bot_token=bot_token,
                chat_id=chat_id,
                label="scheduled_monitor_no_changes",
                reply_markup=NOTIFICATION_KEYBOARD,
            )
        if result.scrape.failed:
            failed = ", ".join(
                SOURCE_LABEL_SHORT.get(source, source)
                for source in result.scrape.failed
            )
            send_telegram_messages(
                [
                    "⚠️ <b>Coleta concluída parcialmente.</b>\n"
                    f"Falha em: {html.escape(failed)}. O último catálogo válido foi preservado."
                ],
                bot_token=bot_token,
                chat_id=chat_id,
                label="scheduled_monitor_partial",
                reply_markup=NOTIFICATION_KEYBOARD,
            )

    try:
        from .html_generator import write_dashboard
        with connect(DEFAULT_DB) as conn:
            write_dashboard(conn, DEFAULT_DB.parent / "produtos.html")
    except Exception:
        log.exception("Falha ao atualizar dashboard após coleta agendada")

    with connect(DEFAULT_DB) as conn:
        set_scheduler_slot(conn, "catalog_monitor", slot)
    log.info(
        "Coleta agendada %s concluída: modo=%s produtos=%d falhas=%s",
        slot, mode, len(result.scrape.products), result.scrape.failed or "nenhuma",
    )
    return True


async def _scheduled_monitor_loop() -> None:
    initial_delay = _env_positive_int("MONITOR_INITIAL_DELAY_SECONDS", 20)
    poll_seconds = _env_positive_int("MONITOR_POLL_SECONDS", 60)
    await asyncio.sleep(initial_delay)
    while True:
        try:
            slot, mode = _latest_due_monitor_slot()
            await _run_sync(run_scheduled_monitor, slot, mode)
        except RuntimeError as exc:
            # Um clique manual pode estar usando o mesmo lock; o slot permanece
            # pendente e será tentado novamente no próximo ciclo.
            log.warning("Coleta agendada adiada: %s", exc)
        except Exception:
            log.exception("Falha na coleta agendada de catálogos")
        await asyncio.sleep(poll_seconds)


def run_daily_promos_task(
    bot_token: str,
    chat_id: str,
    category: str = "tudo",
    message_id: int | None = None,
):
    """Lê do banco o que entrou em promoção nas últimas 24h e filtra por categoria."""
    cat_labels = {
        "tenis": "Tênis/Calçados",
        "vestuario": "Roupas em Geral",
        "camisas_time": "Camisas de Time",
        "agasalhos": "Agasalhos",
        "acessorios": "Acessórios",
        "tudo": "Todas as Peças",
        "50off": "Acima de 50% OFF",
        "ate100": "Até R$ 100"
    }
    label = cat_labels.get(category, "Todas as Peças")
    
    if message_id is None:
        try:
            _render_text_sync(
                bot_token,
                chat_id,
                None,
                f"🌟 <b>Buscando as promoções de hoje ({label})...</b>",
            )
        except Exception:
            pass

    try:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=24)
        since_iso = since_dt.isoformat(timespec="seconds")
        
        with connect(DEFAULT_DB) as conn:
            changes = find_changes(conn, since_iso)
            
        new_promos = changes.get("new_promo", [])
        filtered_promos = []
        
        for row in new_promos:
            price = row["price"]
            list_price = row["list_price"]
            product_category = categorize(row["name"])
            
            if category == "tenis":
                if product_category == "tenis":
                    filtered_promos.append(row)
            elif category == "vestuario":
                if product_category in {"vestuario", "camisas_time", "agasalhos"}:
                    filtered_promos.append(row)
            elif category == "acessorios":
                if product_category == "acessorios":
                    filtered_promos.append(row)
            elif category in {"camisas_time", "agasalhos"}:
                if product_category == category:
                    filtered_promos.append(row)
            elif category == "50off":
                if list_price and price:
                    discount_pct = (1 - float(price) / float(list_price)) * 100
                    if discount_pct >= 50.0:
                        filtered_promos.append(row)
            elif category == "ate100":
                if price and float(price) <= 100.0:
                    filtered_promos.append(row)
            else:  # tudo
                filtered_promos.append(row)
                
        only_new = {"new_promo": filtered_promos}
        
        if not filtered_promos:
            _render_text_sync(
                bot_token,
                chat_id,
                message_id,
                f"Nenhuma nova promoção de {label} registrada nas últimas 24 horas. 😢",
                reply_markup=CATEGORY_KEYBOARD,
            )
            return

        if message_id is not None:
            messages = build_summary(
                only_new, period_label=f"Últimas 24h ({label})"
            )
            text = messages[0]
            if len(messages) > 1:
                text += (
                    "\n\n<i>Resultado resumido para manter a navegação em uma "
                    "mensagem.</i>"
                )
            _render_text_sync(
                bot_token,
                chat_id,
                message_id,
                text,
                reply_markup=CATEGORY_KEYBOARD,
                disable_web_page_preview=True,
            )
        else:
            send_digest(
                only_new,
                period_label=f"Últimas 24h ({label})",
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=NOTIFICATION_KEYBOARD,
            )

    except Exception as e:
        log.exception("Erro interno ao ler promoções do dia")
        try:
            _render_text_sync(
                bot_token,
                chat_id,
                message_id,
                f"❌ <b>Erro interno:</b>\n<pre>{html.escape(str(e))}</pre>",
                reply_markup=CATEGORY_KEYBOARD,
            )
        except Exception:
            pass

def run_scraper_task(sources: list[str] | None, is_snapshot: bool, bot_token: str,
                     chat_id: str, update_key: str | None = None,
                     progress_message_id: int | None = None):
    """Executa o processo de scraping de forma síncrona dentro de um worker thread/background task
    para não bloquear o event loop do FastAPI.
    """
    label = "todas as lojas" if not sources else ", ".join(sources)
    msg = f"🔄 <b>Iniciando varredura de {label}...</b>"
    if is_snapshot:
        msg = "📊 <b>Iniciando geração de Snapshot completo...</b>"

    # Legacy callers do not create an editable progress message in the webhook.
    if progress_message_id is None:
        try:
            response = httpx.post(
                f"{API_BASE}/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10.0,
            )
            if response.status_code == 200:
                progress_message_id = response.json().get("result", {}).get("message_id")
        except Exception as e:
            log.error("Erro ao enviar mensagem inicial: %s", e)

    # Executa a CLI
    started = time.monotonic()
    progress = _progress_editor(
        bot_token, chat_id, progress_message_id, started
    )
    try:
        service = MonitorService(DEFAULT_DB)
        if is_snapshot:
            result = run_exclusive(
                lambda: service.snapshot(sources=sources, progress=progress)
            )
            if result.total_promotions:
                send_digest(
                    result.changes,
                    period_label=datetime.now(timezone.utc).strftime("snapshot %d/%m %Hh UTC"),
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=NOTIFICATION_KEYBOARD,
                )
        else:
            result = run_exclusive(
                lambda: service.run(
                    sources=sources,
                    mode="alert",
                    digest_hours=24,
                    progress=progress,
                )
            )
            if result.total_changes:
                from .notifier import send_alert
                send_alert(
                    result.changes,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=NOTIFICATION_KEYBOARD,
                )

        try:
            from .html_generator import write_dashboard
            with connect(DEFAULT_DB) as conn:
                write_dashboard(conn, DEFAULT_DB.parent / "produtos.html")
        except Exception:
            log.exception("Falha ao atualizar dashboard após tarefa do bot")

        failed_labels = [
            SOURCE_LABEL_SHORT.get(source, source)
            for source in result.scrape.failed
        ]
        if failed_labels:
            log.warning("Scraper finalizou com falhas: %s", ", ".join(result.scrape.failed))

        completion = None
        if is_snapshot and not result.total_promotions:
            completion = "✅ <b>Snapshot concluído.</b>\nNenhuma promoção ativa foi encontrada."
        elif not is_snapshot and not result.total_changes:
            completion = (
                "✅ <b>Varredura concluída.</b>\n"
                "Nenhuma mudança de preço ou promoção foi encontrada."
            )

        if failed_labels:
            warning = "⚠️ Falha em: " + ", ".join(failed_labels) + "."
            completion = f"{completion}\n\n{warning}" if completion else warning

        if completion and not progress_message_id:
            send_telegram_messages(
                [completion],
                bot_token=bot_token,
                chat_id=chat_id,
                label="scraper_completion",
                reply_markup=NOTIFICATION_KEYBOARD,
            )
        elapsed = max(1, int(time.monotonic() - started))
        if progress_message_id:
            attempted = len(sources) if sources else len(SOURCES)
            succeeded = max(0, attempted - len(result.scrape.failed))
            failed = len(result.scrape.failed)
            _edit_progress(
                bot_token,
                chat_id,
                progress_message_id,
                f"✅ <b>Atualização concluída em {elapsed}s.</b>\n"
                f"{succeeded} fonte(s) atualizada(s) · {failed} falha(s).",
                MENU_KEYBOARD,
            )
    except Exception as e:
        log.exception("Erro interno ao rodar tarefa de scraping")
        error_text = format_error_message("Erro interno na atualização", e)
        if progress_message_id:
            _edit_progress(
                bot_token, chat_id, progress_message_id, error_text, MENU_KEYBOARD,
            )
        else:
            try:
                send_telegram_messages(
                    [error_text], bot_token=bot_token, chat_id=chat_id,
                    label="scraper_error", reply_markup=NOTIFICATION_KEYBOARD,
                )
            except Exception as msg_err:
                log.error("Não foi possível notificar erro ao usuário: %s", msg_err)
    finally:
        if update_key:
            _release_update(update_key)


def _source_freshness_message(source: str) -> str:
    freshness = CatalogService(DEFAULT_DB).source_freshness(source)
    return build_freshness_message(
        freshness or source,
        successful=False if freshness is None else None,
        fresh_hours=_env_positive_int("CATALOG_FRESH_HOURS", 2),
        expired_hours=_env_positive_int("CATALOG_STALE_HOURS", 8),
    )


def _needs_catalog_refresh(freshness) -> bool:
    if freshness is None or not freshness.has_snapshot:
        return True
    if freshness.status != "success":
        return True
    age_seconds = freshness.age_seconds
    if age_seconds is None:
        return True
    return age_seconds > _env_positive_int("CATALOG_FRESH_HOURS", 2) * 3600


def _filtered_keyboard(source: str, *, offset: int = 0,
                       has_more: bool = False,
                       favorites: list[tuple[int, str, bool]] | None = None) -> dict:
    rows = []
    for position, ref, selected in favorites or []:
        button = build_favorite_button(ref, selected=selected)
        button["text"] = f"{button['text']} · item {position}"
        rows.append([button])
    navigation = []
    if offset > 0:
        navigation.append({
            "text": "⬅️ Anteriores",
            "callback_data": callback_data(
                "offers", source, max(0, offset - CATALOG_PAGE_SIZE)
            ),
        })
    if has_more:
        navigation.append({
            "text": "Próximas ➡️",
            "callback_data": callback_data(
                "offers", source, offset + CATALOG_PAGE_SIZE
            ),
        })
    if navigation:
        rows.append(navigation)
    rows.extend([
        [
            {"text": "🔄 Verificar agora", "callback_data": callback_data("filter", source, "refresh")},
            {"text": "🎛️ Alterar filtros", "callback_data": callback_data("run", source)},
        ],
        [{"text": "🔙 Lojas", "callback_data": callback_data("stores", "menu")}],
    ])
    return {"inline_keyboard": rows}


def run_filtered_task(source: str, filters: dict, bot_token: str, chat_id: str,
                      offset: int = 0, message_id: int | None = None):
    """Query the last complete successful snapshot; never scrape implicitly."""
    src_label = SOURCE_LABEL_SHORT.get(source, source)
    try:
        catalog = CatalogService(DEFAULT_DB)
        rows = catalog.latest_discounted(
            source=source,
            filters=ProductFilters.from_mapping(filters),
        )
        freshness = _source_freshness_message(source)

        if not rows:
            _render_text_sync(
                bot_token,
                chat_id,
                message_id,
                f"🔎 <b>{src_label}</b>\n{freshness}\n\n"
                "Nenhuma oferta corresponde aos filtros escolhidos.",
                reply_markup=_filtered_keyboard(source),
            )
            return

        total = len(rows)
        last_page_offset = ((total - 1) // CATALOG_PAGE_SIZE) * CATALOG_PAGE_SIZE
        offset = min(max(0, int(offset)), last_page_offset)
        page_rows = rows[offset:offset + CATALOG_PAGE_SIZE]
        has_more = offset + CATALOG_PAGE_SIZE < total

        with connect(DEFAULT_DB) as conn:
            favorite_keys = {
                (str(row["source"]), str(row["sku"]))
                for row in list_favorites(conn, chat_id)
            }
            favorite_buttons = []
            for position, row in enumerate(page_rows, start=offset + 1):
                try:
                    row_source, row_sku = row["source"], row["sku"]
                except (KeyError, IndexError):
                    # Mantem compatibilidade com adaptadores/test doubles
                    # antigos; linhas reais do CatalogService sempre têm ambos.
                    continue
                ref = get_or_create_product_ref(conn, row_source, row_sku)
                key = (str(row_source), str(row_sku))
                favorite_buttons.append((position, ref, key in favorite_keys))
            save_bot_session(
                conn,
                chat_id,
                {**filters, "source": source, "screen": "offers", "offset": offset},
                ui_message_id=message_id,
            )

        # Formata somente uma página para manter a resposta curta e navegável.
        lines = []
        for r in page_rows:
            raw_name = str(r["name"])
            if len(raw_name) > 160:
                raw_name = raw_name[:159].rstrip() + "…"
            name = html.escape(raw_name)
            price = r["price"]
            list_price = r["list_price"]
            raw_url = str(r["url"])
            url = html.escape(raw_url, quote=True)
            pct = int(round((1 - price / list_price) * 100)) if list_price else 0
            intensity = discount_intensity_emoji(pct)
            # URLs aberrantemente longas não podem fazer uma página ultrapassar
            # o limite do Telegram; nesse caso o nome continua legível sem link.
            product_title = (
                f'<b><a href="{url}">{name}</a></b>'
                if len(raw_url) <= 500
                else f"<b>{name}</b>"
            )
            lines.append(
                f"{product_title}\n"
                f"   💰 <b>{format_brl(price)}</b> <s>{format_brl(list_price)}</s> {intensity} <b>-{pct}%</b>"
            )

        first = offset + 1
        last = offset + len(page_rows)
        header = (
            f"<b>{src_label} — ofertas {first}–{last} de {total}</b>\n{freshness}"
        )

        current = header
        for line in lines:
            candidate = current + "\n\n" + line
            # A página pequena + nomes truncados deve caber com folga. Esta
            # guarda evita que dados anômalos façam o Telegram rejeitar tudo.
            if len(candidate) <= 3800:
                current = candidate

        _render_text_sync(
            bot_token,
            chat_id,
            message_id,
            current,
            reply_markup=_filtered_keyboard(
                source, offset=offset, has_more=has_more,
                favorites=favorite_buttons,
            ),
            disable_web_page_preview=True,
        )

    except Exception as e:
        log.exception("Erro ao consultar produtos filtrados")
        try:
            _render_text_sync(
                bot_token,
                chat_id,
                message_id,
                "❌ <b>Erro ao buscar produtos:</b>\n"
                f"<pre>{html.escape(str(e))}</pre>",
                reply_markup=_filtered_keyboard(source),
            )
        except Exception:
            pass


def run_filtered_refresh_task(source: str, filters: dict, bot_token: str,
                              chat_id: str, progress_message_id: int | None) -> None:
    """Refresh one complete source and automatically reapply selected filters."""
    src_label = SOURCE_LABEL_SHORT.get(source, source)
    started = time.monotonic()
    progress = _progress_editor(
        bot_token, chat_id, progress_message_id, started
    )
    try:
        result = run_exclusive(
            lambda: MonitorService(DEFAULT_DB).run(
                sources=[source], mode="alert", digest_hours=24,
                progress=progress,
            )
        )
        elapsed = max(1, int(time.monotonic() - started))
        if result.scrape.failed:
            text = (
                f"⚠️ <b>{src_label} não pôde ser atualizado.</b>\n"
                f"Mantive o último catálogo válido ({elapsed}s)."
            )
        else:
            text = (
                f"✅ <b>{src_label} atualizado em {elapsed}s.</b>\n"
                "Aplicando seus filtros ao novo catálogo…"
            )
        _edit_progress(bot_token, chat_id, progress_message_id, text)
        run_filtered_task(
            source, filters, bot_token, chat_id,
            message_id=progress_message_id,
        )
    except Exception as exc:
        log.exception("Erro ao atualizar %s", source)
        error_text = format_error_message(f"Erro ao atualizar {src_label}", exc)
        if progress_message_id:
            _edit_progress(
                bot_token, chat_id, progress_message_id, error_text,
                _filtered_keyboard(source),
            )
        else:
            try:
                send_telegram_messages(
                    [error_text], bot_token=bot_token, chat_id=chat_id,
                    label="filtered_refresh_error",
                    reply_markup=_filtered_keyboard(source),
                )
            except Exception:
                log.exception("Falha ao notificar erro da atualização filtrada")
    finally:
        _release_update(source)


def get_store_status() -> str:
    """Get status of each store from the database."""
    try:
        catalog = CatalogService(DEFAULT_DB)
        status_by_source = {
            row["source"]: row
            for row in catalog.store_status()
        }
        freshness_by_source = {item.source: item for item in catalog.freshness()}

        lines = ["📊 <b>Status das lojas</b>", ""]
        for source in SOURCES:
            row = status_by_source.get(source)
            freshness = freshness_by_source.get(source)
            src_label = SOURCE_LABEL_SHORT[source]
            if row is None or freshness is None or not freshness.has_snapshot:
                lines.append(f"{src_label}: <i>sem dados coletados</i>")
                continue
            products = row["products"]
            relative = format_relative_time(freshness.last_success_at)
            warning = " · ⚠️ última tentativa falhou" if freshness.status == "failed" else ""
            lines.append(
                f"{src_label}: <b>{products}</b> produtos · verificado {relative}{warning}"
            )

        return "\n".join(lines)
    except Exception as e:
        return format_error_message("Erro ao consultar status", e)


def get_db_stats() -> str:
    """Get database statistics."""
    try:
        stats = CatalogService(DEFAULT_DB).db_stats()
        db_size = int(stats["db_size"])
        if db_size > 1024 * 1024:
            size_str = f"{db_size / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{db_size / 1024:.1f} KB"

        lines = [
            "🗄️ <b>Estatísticas do Banco de Dados</b>",
            "",
            f"📦 Produtos: <b>{stats['total_products']}</b>",
            f"📈 Observações: <b>{stats['total_observations']}</b>",
            f"🏷️ Em promoção: <b>{stats['active_discounts']}</b>",
            f"💾 Tamanho: <b>{size_str}</b>",
        ]

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Erro ao consultar estatísticas: {str(e)}"


def get_top_discounts(limit: int = 10) -> str:
    """Get top discounts across all stores."""
    try:
        rows = CatalogService(DEFAULT_DB).latest_discounted(limit=limit)

        if not rows:
            return "📈 <b>Nenhum desconto encontrado.</b>"

        lines = [f"📈 <b>Top {len(rows)} Descontos</b>", ""]
        for i, r in enumerate(rows, 1):
            name = r["name"][:40]
            pct = r["discount_pct"]
            src_label = SOURCE_LABEL_SHORT.get(r["source"], r["source"])
            lines.append(
                f"{i}. {src_label} <b>-{pct}%</b>\n"
                f"   <a href=\"{r['url']}\">{name}</a>\n"
                f"   {format_brl(r['price'])} <s>{format_brl(r['list_price'])}</s>"
            )

        return "\n".join(lines)
    except Exception as e:
        return format_error_message("Erro ao consultar descontos", e)


def run_purge_dry() -> str:
    """Run purge in dry-run mode to show what would be removed."""
    try:
        result = CatalogService(DEFAULT_DB).purge_candidates()
        if not result.candidates:
            return "🧹 <b>Nenhum produto para purgar.</b>"

        lines = [
            f"🧹 <b>Produtos que seriam removidos: {len(result.candidates)}</b>",
            "",
        ]
        for c in result.candidates[:20]:
            lines.append(f"• {c.name[:50]} ({c.source})")
        if len(result.candidates) > 20:
            lines.append(f"\n... e mais {len(result.candidates) - 20} produtos")

        lines.append("\nUse <b>🧹 Purgar (confirmar)</b> para executar.")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Erro ao verificar purga: {str(e)}"


def run_purge_apply() -> str:
    """Actually purge products that don't pass filters."""
    try:
        result = CatalogService(DEFAULT_DB).purge_apply()
        if not result.candidates:
            return "🧹 <b>Nenhum produto para purgar.</b>"
        return f"🧹 <b>{len(result.candidates)} produto(s) removido(s) com sucesso.</b>"
    except Exception as e:
        return f"❌ Erro ao purgar: {str(e)}"


def normalize_catalog_dry() -> str:
    """Dry-run: find stale data to clean without losing product history."""
    try:
        result = CatalogService(DEFAULT_DB).normalize_dry()
        lines = ["🔍 <b>Normalização do Catálogo</b>", ""]
        total = 0

        if result.old_observations:
            lines.append(f"📅 <b>Histórico antigo (90+ dias): {result.old_observations} observações</b>")
            lines.append("  Serão removidas apenas observações antigas, mantendo o produto.")
            total += result.old_observations
            lines.append("")

        if result.stale_products:
            lines.append(f"⏳ <b>Produtos sem atualização há 14+ dias: {result.stale_products}</b>")
            lines.append("  ⚠️ NÃO serão deletados (preserva histórico para comparação).")
            lines.append("  Eles serão reativados quando o scraper encontrá-los novamente.")
            lines.append("")

        if result.bad_price_products:
            lines.append(f"💰 <b>Produtos com preço inválido: {result.bad_price_products}</b>")
            lines.append("  Serão removidos do banco.")
            total += result.bad_price_products
            lines.append("")

        if not result.old_observations and not result.bad_price_products:
            lines.append("✅ <b>Catálogo limpo! Nenhuma ação necessária.</b>")
        else:
            lines.append(f"\n📊 Ações a executar: <b>{total}</b>")
            lines.append("Use <b>🧹 Normalizar (confirmar)</b> para executar.")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Erro ao verificar normalização: {str(e)}"


def normalize_catalog_apply() -> str:
    """Clean old data without removing products."""
    try:
        result = CatalogService(DEFAULT_DB).normalize_apply()
        return f"🧹 <b>{result.removed} registro(s) limpo(s) na normalização.</b>\n\nProdutos mantidos no banco para preservar histórico."
    except Exception as e:
        return f"❌ Erro ao normalizar: {str(e)}"


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/health/ready")
def health_ready():
    """Readiness: DB acessível, diretório de dados gravável, token presente."""
    checks = {
        "db": False,
        "data_writable": False,
        "telegram_token": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
    }
    try:
        with connect(DEFAULT_DB) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["db"] = True
    except Exception as e:
        checks["db_error"] = str(e)
    try:
        DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
        probe = DEFAULT_DB.parent / ".health_write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks["data_writable"] = True
    except Exception as e:
        checks["data_error"] = str(e)
    ready = checks["db"] and checks["data_writable"] and checks["telegram_token"]
    status = "ready" if ready else "not_ready"
    return JSONResponse({"status": status, "checks": checks},
                        status_code=200 if ready else 503)


@app.get("/status")
def status(admin_token: str | None = None, token: str | None = None):
    """Saúde por fonte (tabela source_runs). Protegido por WEBHOOK_ADMIN_TOKEN."""
    expected_admin = os.environ.get("WEBHOOK_ADMIN_TOKEN") or os.environ.get("ADMIN_TOKEN")
    if not expected_admin or (admin_token or token) != expected_admin:
        return JSONResponse({"status": "forbidden"}, status_code=403)
    with connect(DEFAULT_DB) as conn:
        rows = latest_source_runs(conn)
    return {"sources": [dict(row) for row in rows]}


@app.get("/setup-webhook")
def setup_webhook(url: str, admin_token: str | None = None, token: str | None = None):
    """Auxiliar para configurar o webhook do Telegram.
    Ex: GET /setup-webhook?url=https://seu-app-coolify.com&token=...
    Aceita o token via `token` ou `admin_token` (compatibilidade).
    """
    expected_admin = os.environ.get("WEBHOOK_ADMIN_TOKEN") or os.environ.get("ADMIN_TOKEN")
    if not expected_admin or (admin_token or token) != expected_admin:
        return JSONResponse({"status": "forbidden"}, status_code=403)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return {"error": "TELEGRAM_BOT_TOKEN não está definido nas variáveis de ambiente"}

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return JSONResponse({"error": "url precisa ser HTTPS absoluta"}, status_code=400)

    webhook_url = f"{url.rstrip('/')}/webhook"
    payload = {"url": webhook_url}
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if secret:
        payload["secret_token"] = secret
    log.info("Configurando webhook do Telegram para: %s", webhook_url)

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{API_BASE}/bot{bot_token}/setWebhook",
            json=payload,
        )
    return resp.json()


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Recebe webhooks do Telegram para cliques de botões e mensagens do usuário."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        log.error("TELEGRAM_BOT_TOKEN não configurado no ambiente.")
        return JSONResponse({"status": "error", "message": "Bot token not configured"}, status_code=500)
    if not _check_webhook_secret(request):
        return JSONResponse({"status": "forbidden"}, status_code=403)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid_json"}, status_code=400)

    # 1. Tratar cliques de botões inline (Callback Queries)
    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback["id"]
        raw_callback_data = callback.get("data", "")
        message = callback.get("message")
        if not message:
            return JSONResponse({"status": "no_message_in_callback"})

        chat_id = message["chat"]["id"]
        message_id = message.get("message_id")
        if not _is_allowed_chat(chat_id):
            log.warning("Callback bloqueado de chat não autorizado: %s", chat_id)
            return JSONResponse({"status": "forbidden"}, status_code=403)

        # Responde ao Telegram imediatamente para remover o estado de carregamento do botão
        try:
            await _telegram_post(
                bot_token,
                "answerCallbackQuery",
                {"callback_query_id": callback_id},
                timeout=5.0,
            )
        except Exception:
            # The action is still safe to process; Telegram may retry otherwise.
            log.warning("Falha ao confirmar callback do Telegram", exc_info=True)

        try:
            callback_data_value = decode_to_legacy(raw_callback_data)
        except ValueError:
            return JSONResponse({"status": "invalid_callback_data"}, status_code=400)

        # Determina a ação
        sources = None
        is_snapshot = False

        # --- Filter menu flow ---
        if callback_data_value == "home:new":
            await _send_text(
                bot_token, chat_id, HOME_TEXT, reply_markup=MENU_KEYBOARD,
            )
            return JSONResponse({"status": "menu_sent"})

        if callback_data_value == "run:back":
            await _edit_or_send_text(
                bot_token, chat_id, message_id, HOME_TEXT,
                reply_markup=MENU_KEYBOARD,
            )
            return JSONResponse({"status": "menu_rendered"})

        if callback_data_value == "stores:menu":
            await _edit_or_send_text(
                bot_token,
                chat_id,
                message_id,
                "🛍️ <b>Catálogo por Loja</b>\n\n"
                "Escolha uma loja e ajuste os filtros para consultar o último catálogo confirmado. "
                "Todas as fontes cadastradas aparecem aqui.",
                reply_markup=STORE_KEYBOARD,
            )
            return JSONResponse({"status": "store_menu_sent"})

        if callback_data_value == "saved:list":
            text, markup = await _run_sync(_saved_filters_view, chat_id)
            await _edit_or_send_text(
                bot_token, chat_id, message_id, text, reply_markup=markup,
            )
            return JSONResponse({"status": "saved_filters_rendered"})

        if callback_data_value.startswith("saved:create:"):
            source = callback_data_value.split(":", 2)[2]
            if source not in SOURCES:
                return JSONResponse({"status": "unknown_source"}, status_code=400)
            state = await _run_sync(_load_filter_state, chat_id, source)
            try:
                await _run_sync(_create_saved_filter, chat_id, source, state)
            except ValueError as exc:
                await _edit_or_send_text(
                    bot_token, chat_id, message_id,
                    f"⚠️ <b>Não foi possível salvar:</b> {html.escape(str(exc))}",
                    reply_markup=(await _run_sync(_filter_view, source, state))[1],
                )
                return JSONResponse({"status": "saved_filter_rejected"})
            text, markup = await _run_sync(_filter_view, source, state)
            await _edit_or_send_text(
                bot_token, chat_id, message_id,
                text + "\n\n✅ <b>Filtro salvo.</b>", reply_markup=markup,
            )
            return JSONResponse({"status": "saved_filter_created"})

        if callback_data_value.startswith("saved:load:"):
            raw_id = callback_data_value.split(":", 2)[2]
            try:
                filter_id = int(raw_id)
            except ValueError:
                return JSONResponse({"status": "invalid_saved_filter"}, status_code=400)
            state = await _run_sync(_load_saved_filter_state, chat_id, filter_id)
            if state is None or state["source"] not in SOURCES:
                text, markup = await _run_sync(
                    _saved_filters_view, chat_id, "⚠️ Esse filtro não existe mais."
                )
                await _edit_or_send_text(
                    bot_token, chat_id, message_id, text, reply_markup=markup,
                )
                return JSONResponse({"status": "saved_filter_missing"})
            await _edit_or_send_text(
                bot_token, chat_id, message_id,
                "🔎 <b>Abrindo seu filtro salvo…</b>",
            )
            background_tasks.add_task(
                run_filtered_task, state["source"], dict(state), bot_token,
                str(chat_id), 0, message_id,
            )
            return JSONResponse({"status": "saved_filter_queued"})

        if callback_data_value.startswith("saved:delete:"):
            raw_id = callback_data_value.split(":", 2)[2]
            try:
                filter_id = int(raw_id)
            except ValueError:
                return JSONResponse({"status": "invalid_saved_filter"}, status_code=400)
            removed = await _run_sync(_remove_saved_filter, chat_id, filter_id)
            note = "✅ Filtro removido." if removed else "⚠️ Esse filtro não existe mais."
            text, markup = await _run_sync(_saved_filters_view, chat_id, note)
            await _edit_or_send_text(
                bot_token, chat_id, message_id, text, reply_markup=markup,
            )
            return JSONResponse({"status": "saved_filter_deleted"})

        if callback_data_value.startswith("favorites:page:"):
            raw_page = callback_data_value.split(":", 2)[2]
            try:
                page = max(0, int(raw_page))
            except ValueError:
                return JSONResponse({"status": "invalid_favorites_page"}, status_code=400)
            text, markup = await _run_sync(_favorites_view, chat_id, page)
            await _edit_or_send_text(
                bot_token, chat_id, message_id, text,
                reply_markup=markup, disable_web_page_preview=True,
            )
            return JSONResponse({"status": "favorites_rendered"})

        if callback_data_value.startswith("favorite:toggle:"):
            ref = callback_data_value.split(":", 2)[2]
            selected = await _run_sync(_change_favorite, chat_id, ref)
            if selected is None:
                return JSONResponse({"status": "favorite_missing"}, status_code=404)
            state = await _run_sync(_load_bot_state, chat_id)
            source = state.get("source")
            if source in SOURCES and state.get("screen") == "offers":
                background_tasks.add_task(
                    run_filtered_task, source, dict(state), bot_token,
                    str(chat_id), max(0, int(state.get("offset", 0))), message_id,
                )
            else:
                text, markup = await _run_sync(
                    _favorites_view, chat_id, max(0, int(state.get("page", 0)))
                )
                await _edit_or_send_text(
                    bot_token, chat_id, message_id, text, reply_markup=markup,
                    disable_web_page_preview=True,
                )
            return JSONResponse({
                "status": "favorite_added" if selected else "favorite_removed",
            })

        if callback_data_value.startswith("favorite:delete:"):
            ref = callback_data_value.split(":", 2)[2]
            removed = await _run_sync(_change_favorite, chat_id, ref, remove=True)
            if removed is None:
                return JSONResponse({"status": "favorite_missing"}, status_code=404)
            state = await _run_sync(_load_bot_state, chat_id)
            text, markup = await _run_sync(
                _favorites_view, chat_id, max(0, int(state.get("page", 0)))
            )
            await _edit_or_send_text(
                bot_token, chat_id, message_id, text, reply_markup=markup,
                disable_web_page_preview=True,
            )
            return JSONResponse({"status": "favorite_deleted"})

        if callback_data_value == "alerts:prefs":
            text, markup = await _run_sync(_alert_preferences_view, chat_id)
            await _edit_or_send_text(
                bot_token, chat_id, message_id, text, reply_markup=markup,
            )
            return JSONResponse({"status": "alert_preferences_rendered"})

        if callback_data_value == "alerts:toggle":
            preference = await _run_sync(_get_alert_preference_state, chat_id)
            text, markup = await _run_sync(
                _alert_preferences_view, chat_id,
                enabled=not preference["enabled"],
            )
            await _edit_or_send_text(
                bot_token, chat_id, message_id, text, reply_markup=markup,
            )
            return JSONResponse({"status": "alert_preferences_updated"})

        if callback_data_value.startswith("alerts:hour:"):
            raw_hour = callback_data_value.split(":", 2)[2]
            try:
                hour = int(raw_hour)
            except ValueError:
                return JSONResponse({"status": "invalid_alert_hour"}, status_code=400)
            if not 0 <= hour <= 23:
                return JSONResponse({"status": "invalid_alert_hour"}, status_code=400)
            text, markup = await _run_sync(
                _alert_preferences_view, chat_id, hour_utc=hour,
            )
            await _edit_or_send_text(
                bot_token, chat_id, message_id, text, reply_markup=markup,
            )
            return JSONResponse({"status": "alert_preferences_updated"})

        if callback_data_value == "load:more":
            pending = PENDING_MESSAGES_CACHE.pop(chat_id, None)
            if not pending:
                await _send_text(bot_token, chat_id, "Nada mais para mostrar.")
                return JSONResponse({"status": "no_pending"})

            remaining = pending["messages"]
            reply_markup = pending["reply_markup"]
            first_batch = remaining[:MAX_MESSAGES_PER_BATCH]
            sent = await _run_sync(
                send_telegram_batch, first_batch, bot_token=bot_token,
                chat_id=chat_id, reply_markup=None,
            )

            if len(remaining) > MAX_MESSAGES_PER_BATCH:
                PENDING_MESSAGES_CACHE[chat_id] = {
                    **pending,
                    "messages": remaining[MAX_MESSAGES_PER_BATCH:],
                }
                still_left = len(remaining) - MAX_MESSAGES_PER_BATCH
                summary = (
                    f"📋 <b>Resumo do que falta:</b>\n"
                    f"Enviadas mais {sent}. Faltam <b>{still_left}</b> mensagem(ns)."
                )
                continue_keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "📥 Continuar", "callback_data": callback_data("load", "more")},
                            {"text": "❌ Cancelar", "callback_data": callback_data("load", "cancel")},
                        ]
                    ]
                }
                await _run_sync(
                    send_telegram_batch, [summary], bot_token=bot_token,
                    chat_id=chat_id, reply_markup=continue_keyboard,
                )
            else:
                if reply_markup:
                    await _run_sync(
                        send_telegram_batch,
                        ["✅ Todas as mensagens foram enviadas."],
                        bot_token=bot_token, chat_id=chat_id,
                        reply_markup=reply_markup,
                    )

            return JSONResponse({"status": "load_more_done"})

        if callback_data_value == "load:cancel":
            PENDING_MESSAGES_CACHE.pop(chat_id, None)
            await _edit_or_send_text(
                bot_token, chat_id, message_id,
                "Cancelado.\n\n" + HOME_TEXT,
                reply_markup=MENU_KEYBOARD,
            )
            return JSONResponse({"status": "load_cancelled"})

        if callback_data_value.startswith("offers:"):
            parts = callback_data_value.split(":")
            if len(parts) != 3 or parts[1] not in SOURCES:
                return JSONResponse(
                    {"status": "invalid_offers_callback"}, status_code=400,
                )
            try:
                offset = max(0, int(parts[2]))
            except ValueError:
                return JSONResponse(
                    {"status": "invalid_offers_offset"}, status_code=400,
                )
            source = parts[1]
            state = await _run_sync(_load_filter_state, chat_id, source)
            background_tasks.add_task(
                run_filtered_task, source, dict(state), bot_token,
                str(chat_id), offset, message_id,
            )
            return JSONResponse({"status": "offers_page_queued"})

        if callback_data_value.startswith("filter:"):
            parts = callback_data_value.split(":")
            if len(parts) < 3:
                return JSONResponse({"status": "invalid_filter_callback"}, status_code=400)
            source = parts[1]
            action = parts[2]
            value = parts[3] if len(parts) > 3 else None
            valid_values = {
                "cat": {"tenis", "vestuario", "acessorios", "camisas_time", "agasalhos", "all"},
                "price": {"100", "200", "500", "all"},
                "disc": {"50", "30", "all"},
            }
            if source not in SOURCES:
                return JSONResponse({"status": "unknown_source"}, status_code=400)
            if action not in ({"run", "refresh"} | set(valid_values)):
                return JSONResponse({"status": "invalid_filter_action"}, status_code=400)
            if action not in {"run", "refresh"} and value not in valid_values[action]:
                return JSONResponse({"status": "invalid_filter_value"}, status_code=400)

            state = await _run_sync(_load_filter_state, chat_id, source)

            if action == "cat":
                state["category"] = value
            elif action == "price":
                state["max_price"] = value
            elif action == "disc":
                state["min_discount"] = value
            elif action == "run":
                await _edit_or_send_text(
                    bot_token, chat_id, message_id,
                    "🔎 <b>Consultando o último catálogo confirmado…</b>",
                )
                background_tasks.add_task(
                    run_filtered_task, source, dict(state), bot_token,
                    str(chat_id), 0, message_id,
                )
                freshness = await _run_sync(
                    CatalogService(DEFAULT_DB).source_freshness, source,
                )
                if _needs_catalog_refresh(freshness) and _reserve_update(source):
                    # BackgroundTasks preserva a ordem: envia primeiro o
                    # snapshot rápido e depois revalida a fonte inteira.
                    background_tasks.add_task(
                        run_filtered_refresh_task, source, dict(state),
                        bot_token, str(chat_id), message_id,
                    )
                    return JSONResponse({
                        "status": "filtered_query_and_refresh_queued",
                    })
                return JSONResponse({"status": "filtered_query_queued"})
            elif action == "refresh":
                if not _reserve_update(source):
                    await _edit_or_send_text(
                        bot_token, chat_id, message_id,
                        f"⏳ <b>{SOURCE_LABEL_SHORT.get(source, source)}</b> já está sendo atualizado.",
                        reply_markup=_filtered_keyboard(source),
                    )
                    return JSONResponse({"status": "update_already_running"})
                await _edit_or_send_text(
                    bot_token, chat_id, message_id,
                    build_progress_message(0, 1, current=source),
                )
                background_tasks.add_task(
                    run_filtered_refresh_task, source, dict(state), bot_token,
                    str(chat_id), message_id,
                )
                return JSONResponse({"status": "filtered_refresh_queued"})

            state["screen"] = "filters"
            await _run_sync(
                _save_filter_state, chat_id, state, message_id=message_id,
            )
            filter_text, filter_markup = await _run_sync(_filter_view, source, state)
            await _edit_or_send_text(
                bot_token, chat_id, message_id,
                filter_text,
                reply_markup=filter_markup,
            )
            return JSONResponse({"status": "filter_updated"})

        # --- Explicit catalog updates (queries never scrape implicitly) ---
        if callback_data_value == "update:menu":
            await _edit_or_send_text(
                bot_token,
                chat_id,
                message_id,
                "🔄 <b>Atualizar catálogo</b>\n\n"
                "Escolha uma loja. A coleta completa roda em segundo plano.",
                reply_markup=UPDATE_KEYBOARD,
            )
            return JSONResponse({"status": "update_menu_sent"})

        if callback_data_value.startswith("update:"):
            target = callback_data_value.split(":", 1)[1]
            if target != "all" and target not in SOURCES:
                return JSONResponse({"status": "unknown_update_source"}, status_code=400)
            update_key = "*" if target == "all" else target
            if not _reserve_update(update_key):
                await _edit_or_send_text(
                    bot_token, chat_id, message_id,
                    "⏳ Já existe uma atualização incompatível em andamento. "
                    "Aguarde a conclusão para evitar coletas duplicadas.",
                    reply_markup=UPDATE_KEYBOARD,
                )
                return JSONResponse({"status": "update_already_running"})
            await _edit_or_send_text(
                bot_token, chat_id, message_id,
                build_progress_message(
                    0, len(SOURCES) if target == "all" else 1,
                    current=None if target == "all" else target,
                ),
            )
            background_tasks.add_task(
                run_scraper_task,
                None if target == "all" else [target],
                False,
                bot_token,
                str(chat_id),
                update_key,
                message_id,
            )
            return JSONResponse({"status": "update_queued"})

        # --- Catalog management flow ---
        if callback_data_value == "catalog:menu":
            await _edit_or_send_text(
                bot_token, chat_id, message_id,
                "⚙️ <b>Gerenciar Catálogo</b>\n\nEscolha uma opção:",
                reply_markup=CATALOG_KEYBOARD,
            )
            return JSONResponse({"status": "catalog_menu_sent"})

        if callback_data_value == "catalog:status":
            status_text = await _run_sync(get_store_status)
            await _edit_or_send_text(
                bot_token, chat_id, message_id, status_text,
                reply_markup=CATALOG_KEYBOARD,
            )
            return JSONResponse({"status": "catalog_status_sent"})

        if callback_data_value == "catalog:db_stats":
            stats_text = await _run_sync(get_db_stats)
            await _edit_or_send_text(
                bot_token, chat_id, message_id, stats_text,
                reply_markup=CATALOG_KEYBOARD,
            )
            return JSONResponse({"status": "catalog_stats_sent"})

        if callback_data_value == "catalog:top_discounts":
            discounts_text = await _run_sync(get_top_discounts)
            await _edit_or_send_text(
                bot_token, chat_id, message_id, discounts_text,
                disable_web_page_preview=True, reply_markup=CATALOG_KEYBOARD,
            )
            return JSONResponse({"status": "catalog_discounts_sent"})

        if callback_data_value == "catalog:purge":
            purge_text = await _run_sync(run_purge_dry)
            purge_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🧹 Purgar (confirmar)", "callback_data": callback_data("catalog", "purge_apply")},
                        {"text": "❌ Cancelar", "callback_data": callback_data("catalog", "menu")},
                    ]
                ]
            }
            await _edit_or_send_text(
                bot_token, chat_id, message_id, purge_text,
                reply_markup=purge_keyboard,
            )
            return JSONResponse({"status": "catalog_purge_sent"})

        if callback_data_value == "catalog:purge_apply":
            purge_result = await _run_sync(run_purge_apply)
            await _edit_or_send_text(
                bot_token, chat_id, message_id, purge_result,
                reply_markup=CATALOG_KEYBOARD,
            )
            return JSONResponse({"status": "catalog_purge_applied"})

        if callback_data_value == "catalog:force_update":
            if not _reserve_update("*"):
                await _edit_or_send_text(
                    bot_token, chat_id, message_id,
                    "⏳ Uma atualização já está em andamento.",
                    reply_markup=CATALOG_KEYBOARD,
                )
                return JSONResponse({"status": "update_already_running"})
            await _edit_or_send_text(
                bot_token, chat_id, message_id,
                build_progress_message(0, len(SOURCES)),
            )
            background_tasks.add_task(
                run_scraper_task, None, False, bot_token, str(chat_id), "*", message_id
            )
            return JSONResponse({"status": "force_update_queued"})

        if callback_data_value == "catalog:normalize":
            normalize_text = await _run_sync(normalize_catalog_dry)
            normalize_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🧹 Normalizar (confirmar)", "callback_data": callback_data("catalog", "normalize_apply")},
                        {"text": "❌ Cancelar", "callback_data": callback_data("catalog", "menu")},
                    ]
                ]
            }
            await _edit_or_send_text(
                bot_token, chat_id, message_id, normalize_text,
                reply_markup=normalize_keyboard,
            )
            return JSONResponse({"status": "catalog_normalize_sent"})

        if callback_data_value == "catalog:normalize_apply":
            normalize_result = await _run_sync(normalize_catalog_apply)
            await _edit_or_send_text(
                bot_token, chat_id, message_id, normalize_result,
                reply_markup=CATALOG_KEYBOARD,
            )
            return JSONResponse({"status": "catalog_normalize_applied"})

        # --- Existing flows ---
        if callback_data_value == "run:snapshot":
            is_snapshot = True
        elif callback_data_value == "run:daily_promos":
            await _edit_or_send_text(
                bot_token, chat_id, message_id,
                "Escolha a categoria de promoções de hoje que você quer ver:",
                reply_markup=CATEGORY_KEYBOARD,
            )
            return JSONResponse({"status": "category_menu_sent"})
        elif callback_data_value.startswith("run:daily_promos:"):
            category = callback_data_value.split(":")[-1]
            valid_categories = {
                "tenis", "vestuario", "acessorios", "camisas_time",
                "agasalhos", "tudo", "50off", "ate100",
            }
            if category not in valid_categories:
                return JSONResponse({"status": "unknown_daily_category"}, status_code=400)
            await _edit_or_send_text(
                bot_token, chat_id, message_id,
                "🌟 <b>Consultando as promoções das últimas 24 horas…</b>",
            )
            background_tasks.add_task(
                run_daily_promos_task, bot_token, str(chat_id), category,
                message_id,
            )
            return JSONResponse({"status": "daily_task_queued"})
        elif callback_data_value.startswith("run:"):
            action = callback_data_value.split(":")[1]
            if action == "all":
                pass  # sources = None → all
            elif action not in SOURCES:
                return JSONResponse({"status": "unknown_source"}, status_code=400)
            else:
                # Preserve the selection when returning from a result list.
                current_filters = await _run_sync(_load_filter_state, chat_id, action)
                current_filters["screen"] = "filters"
                await _run_sync(
                    _save_filter_state, chat_id, current_filters,
                    message_id=message_id,
                )
                filter_text, filter_markup = await _run_sync(
                    _filter_view, action, current_filters,
                )
                await _edit_or_send_text(
                    bot_token, chat_id, message_id,
                    filter_text,
                    reply_markup=filter_markup,
                )
                return JSONResponse({"status": "filter_menu_sent"})
        else:
            return JSONResponse({"status": "unknown_callback_data"})

        # Dispara o scraper em segundo plano (background tasks do FastAPI/thread pool)
        background_tasks.add_task(run_scraper_task, sources, is_snapshot, bot_token, str(chat_id))
        return JSONResponse({"status": "task_queued"})

    # 2. Tratar mensagens normais do chat (ex: /start, /menu, ou mensagens de texto comuns)
    if "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        if not _is_allowed_chat(chat_id):
            log.warning("Mensagem bloqueada de chat não autorizado: %s", chat_id)
            return JSONResponse({"status": "forbidden"}, status_code=403)
        text = message.get("text", "")

        if not text:
            return JSONResponse({"status": "ignored"})

        if text.startswith("/start") or text.startswith("/menu"):
            await _run_sync(
                send_menu_message, bot_token=bot_token, chat_id=chat_id
            )
            return JSONResponse({"status": "menu_sent"})

        await _run_sync(
            send_menu_message, bot_token=bot_token, chat_id=chat_id,
            text="Use os botões abaixo para interagir com o monitor.",
        )
        return JSONResponse({"status": "menu_sent"})

    return JSONResponse({"status": "ignored"})
