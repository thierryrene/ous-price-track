from __future__ import annotations

import logging
import os
import sqlite3
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from datetime import datetime, timezone, timedelta
from .cli import DEFAULT_DB
from .notifier import (
    send_menu_message, API_BASE, MENU_KEYBOARD, CATEGORY_KEYBOARD, CATALOG_KEYBOARD,
    send_digest, send_filter_menu, SOURCE_LABEL_SHORT,
    PENDING_MESSAGES_CACHE, send_telegram_batch, MAX_MESSAGES_PER_BATCH,
    send_telegram_messages, format_error_message, format_brl, discount_intensity_emoji,
)
from urllib.parse import urlparse
from .services import CatalogService, MonitorService, ProductFilters, run_exclusive
from .sources import source_keys
from .storage import connect, find_changes, latest_source_runs

log = logging.getLogger("ous_monitor.server")

app = FastAPI(title="OUS Price Monitor Webhook Bot Server")

_filter_state: dict[int, dict] = {}

# Carrega e inicializa o logger básico caso não tenha sido inicializado
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


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
    return not allowed or int(chat_id) in allowed


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
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{API_BASE}/bot{bot_token}/sendMessage",
            json=payload,
            timeout=10.0,
        )


def query_prices_db(sql_query: str) -> str:
    """Executa consultas SQL de leitura (SELECT) no banco de dados SQLite local.
    Útil para recuperar informações de produtos, histórico de preços e descontos.

    Args:
        sql_query: Uma string de consulta SQL SELECT válida.
    """
    try:
        sql = sql_query.strip()
        sql_lower = sql.lower()
        if not (sql_lower.startswith("select") or sql_lower.startswith("with")):
            return "Erro: somente consultas SELECT/WITH de leitura são permitidas."
        if ";" in sql.rstrip(";"):
            return "Erro: apenas uma instrução SQL por consulta é permitida."
        db_uri = f"file:{DEFAULT_DB}?mode=ro"
        with sqlite3.connect(db_uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchmany(100)
            return str([dict(row) for row in rows])
    except Exception as e:
        return f"Erro ao executar query: {str(e)}"


def run_store_scraper(store: str) -> str:
    """Executa a varredura (scraping) de preços em uma loja específica e atualiza o banco de dados.

    Args:
        store: A loja para varrer (qualquer chave de `sources.SOURCES`).
    """
    valid_stores = tuple(source_keys())
    if store not in valid_stores:
        return (f"Erro: Loja desconhecida '{store}'. Escolha entre "
                f"{', '.join(valid_stores)}.")
    try:
        result = run_exclusive(
            lambda: MonitorService(DEFAULT_DB).run(
                sources=[store],
                mode="alert",
                digest_hours=24,
            )
        )
        if result.scrape.products:
            return f"Sucesso! A loja '{store}' foi re-analisada e os preços no banco local foram atualizados."
        return f"Atenção: a loja '{store}' não retornou produtos."
    except Exception as e:
        return f"Falha na execução do scraper da loja '{store}': {str(e)}"


def run_daily_promos_task(bot_token: str, chat_id: str, category: str = "tudo"):
    """Lê do banco o que entrou em promoção nas últimas 24h e filtra por categoria."""
    cat_labels = {
        "tenis": "Tênis/Calçados",
        "vestuario": "Vestuário",
        "acessorios": "Acessórios",
        "tudo": "Todas as Peças",
        "50off": "Acima de 50% OFF",
        "ate100": "Até R$ 100"
    }
    label = cat_labels.get(category, "Todas as Peças")
    
    try:
        httpx.post(
            f"{API_BASE}/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"🌟 <b>Buscando as promoções de hoje ({label})...</b>",
                "parse_mode": "HTML",
            },
            timeout=10.0,
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
            name_lower = row["name"].lower()
            price = row["price"]
            list_price = row["list_price"]
            
            if category == "tenis":
                if "tênis" in name_lower or "tenis" in name_lower or "chinelo" in name_lower:
                    filtered_promos.append(row)
            elif category == "vestuario":
                vest_keywords = ["camiseta", "camisa", "moletom", "jaqueta", "calça", "calca", "bermuda", "short", "meia"]
                if any(kw in name_lower for kw in vest_keywords):
                    filtered_promos.append(row)
            elif category == "acessorios":
                acess_keywords = ["boné", "bone", "gorro", "mochila", "shoulder", "bag", "cinto", "cadarço", "carteira", "óculos", "oculos"]
                if any(kw in name_lower for kw in acess_keywords):
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
            httpx.post(
                f"{API_BASE}/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"Nenhuma nova promoção de {label} registrada nas últimas 24 horas. 😢",
                    "reply_markup": MENU_KEYBOARD,
                },
                timeout=10.0,
            )
            return

        send_digest(only_new, period_label=f"Últimas 24h ({label})", bot_token=bot_token, chat_id=chat_id, reply_markup=MENU_KEYBOARD)

    except Exception as e:
        log.exception("Erro interno ao ler promoções do dia")
        try:
            httpx.post(
                f"{API_BASE}/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"❌ <b>Erro interno:</b>\n<pre>{str(e)}</pre>",
                    "parse_mode": "HTML",
                },
                timeout=10.0,
            )
        except Exception:
            pass

def run_scraper_task(sources: list[str] | None, is_snapshot: bool, bot_token: str, chat_id: str):
    """Executa o processo de scraping de forma síncrona dentro de um worker thread/background task
    para não bloquear o event loop do FastAPI.
    """
    label = "todas as lojas" if not sources else ", ".join(sources)
    msg = f"🔄 <b>Iniciando varredura de {label}...</b>"
    if is_snapshot:
        msg = "📊 <b>Iniciando geração de Snapshot completo...</b>"

    # Envia aviso de início
    try:
        httpx.post(
            f"{API_BASE}/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
            },
            timeout=10.0,
        )
    except Exception as e:
        log.error("Erro ao enviar mensagem inicial: %s", e)

    # Executa a CLI
    try:
        service = MonitorService(DEFAULT_DB)
        if is_snapshot:
            result = run_exclusive(lambda: service.snapshot(sources=sources))
            if result.total_promotions:
                send_digest(
                    result.changes,
                    period_label=datetime.now(timezone.utc).strftime("snapshot %d/%m %Hh UTC"),
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=MENU_KEYBOARD,
                )
        else:
            result = run_exclusive(
                lambda: service.run(
                    sources=sources,
                    mode="alert",
                    digest_hours=24,
                )
            )
            if result.total_changes:
                from .notifier import send_alert
                send_alert(
                    result.changes,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=MENU_KEYBOARD,
                )

        try:
            from .html_generator import write_dashboard
            with connect(DEFAULT_DB) as conn:
                write_dashboard(conn, DEFAULT_DB.parent / "produtos.html")
        except Exception:
            log.exception("Falha ao atualizar dashboard após tarefa do bot")

        if result.scrape.failed:
            log.warning("Scraper finalizou com falhas: %s", ", ".join(result.scrape.failed))
        if not result.scrape.products:
            send_telegram_messages(
                ["Nenhum produto coletado nesta varredura."],
                bot_token=bot_token,
                chat_id=chat_id,
                label="scraper_empty",
                reply_markup=MENU_KEYBOARD,
            )
    except Exception as e:
        log.exception("Erro interno ao rodar tarefa de scraping")
        try:
            send_telegram_messages(
                [format_error_message("Erro interno na execução do scraper", e)],
                bot_token=bot_token,
                chat_id=chat_id,
                label="scraper_error",
                reply_markup=MENU_KEYBOARD,
            )
        except Exception as msg_err:
            log.error("Não foi possível notificar erro ao usuário: %s", msg_err)


def run_filtered_task(source: str, filters: dict, bot_token: str, chat_id: str):
    """Run scraper for one source, then filter results and send to user."""
    src_label = SOURCE_LABEL_SHORT.get(source, source)

    try:
        httpx.post(
            f"{API_BASE}/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"🔄 <b>Varrendo {src_label} com filtros...</b>",
                "parse_mode": "HTML",
            },
            timeout=10.0,
        )
    except Exception:
        pass

    # Run scraper
    try:
        result = run_exclusive(
            lambda: MonitorService(DEFAULT_DB).run(
                sources=[source],
                mode="alert",
                digest_hours=24,
            )
        )
        if result.scrape.failed:
            log.warning("Scraper %s finalizou com falhas: %s", source, result.scrape.failed)
    except Exception as e:
        log.exception("Erro ao rodar scraper %s", source)
        try:
            send_telegram_messages(
                [format_error_message(f"Erro no scraper {src_label}", e)],
                bot_token=bot_token,
                chat_id=chat_id,
                label="filtered_error",
                reply_markup=MENU_KEYBOARD,
            )
        except Exception:
            pass
        return

    # Query DB for latest products from this source with filters
    try:
        rows = CatalogService(DEFAULT_DB).latest_discounted(
            source=source,
            filters=ProductFilters.from_mapping(filters),
        )

        if not rows:
            httpx.post(
                f"{API_BASE}/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"Nenhum produto encontrado em {src_label} com esses filtros. 😢",
                    "reply_markup": MENU_KEYBOARD,
                },
                timeout=10.0,
            )
            return

        # Format results
        lines = []
        for r in rows[:30]:
            name = r["name"]
            price = r["price"]
            list_price = r["list_price"]
            url = r["url"]
            pct = int(round((1 - price / list_price) * 100)) if list_price else 0
            intensity = discount_intensity_emoji(pct)
            lines.append(
                f"🆕 <b><a href=\"{url}\">{name}</a></b>\n"
                f"   💰 <b>{format_brl(price)}</b> <s>{format_brl(list_price)}</s> {intensity} <b>-{pct}%</b>"
            )

        header = f"<b>{src_label} — {len(rows)} produto(s) encontrado(s)</b>"
        if len(rows) > 30:
            header += f"\n<i>(mostrando top 30 por maior desconto)</i>"

        messages_text = []
        current = header
        for line in lines:
            candidate = current + "\n\n" + line
            if len(candidate) > 3800:
                messages_text.append(current)
                current = line
            else:
                current = candidate
        if current.strip():
            messages_text.append(current)

        send_telegram_messages(
            messages_text,
            bot_token=bot_token,
            chat_id=chat_id,
            label="filtered",
            reply_markup=MENU_KEYBOARD,
        )

    except Exception as e:
        log.exception("Erro ao consultar produtos filtrados")
        try:
            httpx.post(
                f"{API_BASE}/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"❌ <b>Erro ao buscar produtos:</b>\n<pre>{str(e)}</pre>",
                    "parse_mode": "HTML",
                },
                timeout=10.0,
            )
        except Exception:
            pass


def get_store_status() -> str:
    """Get status of each store from the database."""
    try:
        sources = CatalogService(DEFAULT_DB).store_status()

        if not sources:
            return "📊 <b>Nenhum dado no banco de dados.</b>"

        lines = ["📊 <b>Status das Lojas</b>", ""]
        for row in sources:
            source = row["source"]
            products = row["products"]
            newest = row["newest"][:16] if row["newest"] else "N/A"
            src_label = SOURCE_LABEL_SHORT.get(source, source)
            lines.append(f"{src_label}: <b>{products}</b> produtos (última: {newest})")

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


async def run_agy_agent_chat(user_message: str, bot_token: str, chat_id: str):
    """Inicializa e executa o agente de IA do Antigravity para interagir com o usuário."""
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": (
                            "❌ <b>GEMINI_API_KEY não encontrada no servidor!</b>\n"
                            "Para conversar por chat e utilizar a inteligência do Agy, configure a variável "
                            "<code>GEMINI_API_KEY</code> no painel do Coolify."
                        ),
                        "parse_mode": "HTML",
                    },
                    timeout=10.0,
                )
        except Exception:
            log.exception("Erro ao alertar usuário sobre GEMINI_API_KEY ausente")
        return

    # Envia ação de "digitando..." para feedback visual no Telegram
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{API_BASE}/bot{bot_token}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
                timeout=5.0,
            )
    except Exception:
        pass

    try:
        from google.antigravity import Agent, LocalAgentConfig

        config = LocalAgentConfig(
            api_key=gemini_api_key,
            model="gemini-1.5-flash",  # Modelo ideal para chat de ferramentas rápido
            tools=[query_prices_db, run_store_scraper],
            system_instructions=(
                "Você é o Agy, o assistente inteligente de IA do Thierry para o monitor de preços de calçados ÖUS. "
                "Você ajuda o Thierry a analisar tendências de preços, encontrar descontos e decidir a melhor hora para comprar. "
                "Você tem acesso ao banco de dados histórico local através da ferramenta query_prices_db. Use-a sempre que precisar "
                "obter dados de preços, tamanhos disponíveis e estoques. "
                "Se o Thierry pedir para atualizar, varrer ou sincronizar uma loja específica, use run_store_scraper. "
                "Sempre responda em português brasileiro de forma amigável, direta e visualmente limpa. "
                "ATENÇÃO: Formate sua resposta em HTML do Telegram usando apenas tags permitidas: <b>, <i>, <code>, <pre> e links <a href='...'>. "
                "Nunca use markdown puro (como asteriscos ** para negrito ou hashtags # para títulos) na sua mensagem final; converta em tags HTML equivalentes. "
                "Assine sua mensagem com '— Agy 🤖'."
            ),
        )

        async with Agent(config) as agent:
            response = await agent.chat(user_message)
            full_response = ""
            async for chunk in response:
                full_response += chunk

            # Envia a resposta final para o chat no Telegram
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": full_response,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": MENU_KEYBOARD,  # Inclui o menu com os botões inline no rodapé
                    },
                    timeout=15.0,
                )
    except Exception as e:
        log.exception("Falha durante execução do agente de IA AGY")
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"❌ <b>Erro no Agente de IA:</b>\n<pre>{str(e)}</pre>",
                        "parse_mode": "HTML",
                    },
                    timeout=10.0,
                )
        except Exception:
            pass


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
        callback_data = callback["data"]
        message = callback.get("message")
        if not message:
            return JSONResponse({"status": "no_message_in_callback"})

        chat_id = message["chat"]["id"]
        if not _is_allowed_chat(chat_id):
            log.warning("Callback bloqueado de chat não autorizado: %s", chat_id)
            return JSONResponse({"status": "forbidden"}, status_code=403)

        # Responde ao Telegram imediatamente para remover o estado de carregamento do botão
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{API_BASE}/bot{bot_token}/answerCallbackQuery",
                json={"callback_query_id": callback_id},
                timeout=5.0,
            )

        # Determina a ação
        sources = None
        is_snapshot = False

        # --- Filter menu flow ---
        if callback_data == "run:back":
            send_menu_message(bot_token=bot_token, chat_id=chat_id)
            _filter_state.pop(chat_id, None)
            return JSONResponse({"status": "menu_sent"})

        if callback_data == "load:more":
            pending = PENDING_MESSAGES_CACHE.pop(chat_id, None)
            if not pending:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{API_BASE}/bot{bot_token}/sendMessage",
                        json={"chat_id": chat_id, "text": "Nada mais para mostrar."},
                        timeout=5.0,
                    )
                return JSONResponse({"status": "no_pending"})

            remaining = pending["messages"]
            reply_markup = pending["reply_markup"]
            first_batch = remaining[:MAX_MESSAGES_PER_BATCH]
            sent = send_telegram_batch(
                first_batch,
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=None,
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
                            {"text": "📥 Continuar", "callback_data": "load:more"},
                            {"text": "❌ Cancelar", "callback_data": "load:cancel"},
                        ]
                    ]
                }
                send_telegram_batch(
                    [summary],
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=continue_keyboard,
                )
            else:
                if reply_markup:
                    send_telegram_batch(
                        ["✅ Todas as mensagens foram enviadas."],
                        bot_token=bot_token,
                        chat_id=chat_id,
                        reply_markup=reply_markup,
                    )

            return JSONResponse({"status": "load_more_done"})

        if callback_data == "load:cancel":
            PENDING_MESSAGES_CACHE.pop(chat_id, None)
            send_menu_message(bot_token=bot_token, chat_id=chat_id,
                              text="Cancelado. Voltando ao menu principal.")
            return JSONResponse({"status": "load_cancelled"})

        if callback_data.startswith("filter:"):
            parts = callback_data.split(":")
            source = parts[1]
            action = parts[2]
            value = parts[3] if len(parts) > 3 else None

            state = _filter_state.get(chat_id)
            if not state or state.get("source") != source:
                state = {"source": source, "category": "all", "max_price": "all", "min_discount": "all"}
                _filter_state[chat_id] = state

            if action == "cat":
                state["category"] = value
            elif action == "price":
                state["max_price"] = value
            elif action == "disc":
                state["min_discount"] = value
            elif action == "run":
                _filter_state.pop(chat_id, None)
                background_tasks.add_task(
                    run_filtered_task, source, state, bot_token, str(chat_id)
                )
                return JSONResponse({"status": "filtered_task_queued"})

            send_filter_menu(bot_token=bot_token, chat_id=chat_id,
                             source=source, filters=state,
                             edit_message_id=message.get("message_id"))
            return JSONResponse({"status": "filter_updated"})

        # --- Catalog management flow ---
        if callback_data == "catalog:menu":
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": "⚙️ <b>Gerenciar Catálogo</b>\n\nEscolha uma opção:",
                        "parse_mode": "HTML",
                        "reply_markup": CATALOG_KEYBOARD,
                    },
                    timeout=5.0,
                )
            return JSONResponse({"status": "catalog_menu_sent"})

        if callback_data == "catalog:status":
            status_text = get_store_status()
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": status_text,
                        "parse_mode": "HTML",
                        "reply_markup": CATALOG_KEYBOARD,
                    },
                    timeout=10.0,
                )
            return JSONResponse({"status": "catalog_status_sent"})

        if callback_data == "catalog:db_stats":
            stats_text = get_db_stats()
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": stats_text,
                        "parse_mode": "HTML",
                        "reply_markup": CATALOG_KEYBOARD,
                    },
                    timeout=10.0,
                )
            return JSONResponse({"status": "catalog_stats_sent"})

        if callback_data == "catalog:top_discounts":
            discounts_text = get_top_discounts()
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": discounts_text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": CATALOG_KEYBOARD,
                    },
                    timeout=10.0,
                )
            return JSONResponse({"status": "catalog_discounts_sent"})

        if callback_data == "catalog:purge":
            purge_text = run_purge_dry()
            purge_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🧹 Purgar (confirmar)", "callback_data": "catalog:purge_apply"},
                        {"text": "❌ Cancelar", "callback_data": "catalog:menu"},
                    ]
                ]
            }
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": purge_text,
                        "parse_mode": "HTML",
                        "reply_markup": purge_keyboard,
                    },
                    timeout=10.0,
                )
            return JSONResponse({"status": "catalog_purge_sent"})

        if callback_data == "catalog:purge_apply":
            purge_result = run_purge_apply()
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": purge_result,
                        "parse_mode": "HTML",
                        "reply_markup": CATALOG_KEYBOARD,
                    },
                    timeout=10.0,
                )
            return JSONResponse({"status": "catalog_purge_applied"})

        if callback_data == "catalog:force_update":
            background_tasks.add_task(run_scraper_task, None, False, bot_token, str(chat_id))
            return JSONResponse({"status": "force_update_queued"})

        if callback_data == "catalog:normalize":
            normalize_text = normalize_catalog_dry()
            normalize_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🧹 Normalizar (confirmar)", "callback_data": "catalog:normalize_apply"},
                        {"text": "❌ Cancelar", "callback_data": "catalog:menu"},
                    ]
                ]
            }
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": normalize_text,
                        "parse_mode": "HTML",
                        "reply_markup": normalize_keyboard,
                    },
                    timeout=10.0,
                )
            return JSONResponse({"status": "catalog_normalize_sent"})

        if callback_data == "catalog:normalize_apply":
            normalize_result = normalize_catalog_apply()
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": normalize_result,
                        "parse_mode": "HTML",
                        "reply_markup": CATALOG_KEYBOARD,
                    },
                    timeout=10.0,
                )
            return JSONResponse({"status": "catalog_normalize_applied"})

        # --- Existing flows ---
        if callback_data == "run:snapshot":
            is_snapshot = True
        elif callback_data == "run:daily_promos":
            from .notifier import CATEGORY_KEYBOARD
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": "Escolha a categoria de promoções de hoje que você quer ver:",
                        "reply_markup": CATEGORY_KEYBOARD
                    },
                    timeout=5.0
                )
            return JSONResponse({"status": "category_menu_sent"})
        elif callback_data.startswith("run:daily_promos:"):
            category = callback_data.split(":")[-1]
            background_tasks.add_task(run_daily_promos_task, bot_token, str(chat_id), category)
            return JSONResponse({"status": "daily_task_queued"})
        elif callback_data.startswith("run:"):
            action = callback_data.split(":")[1]
            if action == "all":
                pass  # sources = None → all
            else:
                # Intercept brand click → show filter menu instead of running directly
                _filter_state[chat_id] = {
                    "source": action,
                    "category": "all",
                    "max_price": "all",
                    "min_discount": "all",
                }
                send_filter_menu(bot_token=bot_token, chat_id=chat_id,
                                 source=action, filters=_filter_state[chat_id])
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
            send_menu_message(bot_token=bot_token, chat_id=chat_id)
            return JSONResponse({"status": "menu_sent"})

        send_menu_message(
            bot_token=bot_token, chat_id=chat_id,
            text="Use os botões abaixo para interagir com o monitor.",
        )
        return JSONResponse({"status": "menu_sent"})

    return JSONResponse({"status": "ignored"})
