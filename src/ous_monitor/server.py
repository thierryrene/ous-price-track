from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import threading
from pathlib import Path
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from .cli import cmd_run, cmd_snapshot, DEFAULT_DB, DEFAULT_ENV
from .notifier import (
    send_menu_message,
    API_BASE,
    MENU_KEYBOARD,
    MAIN_KEYBOARD,
    DB_KEYBOARD,
    SCRAPERS_KEYBOARD,
    send_db_promotions,
)

log = logging.getLogger("ous_monitor.server")

app = FastAPI(title="OUS Price Monitor Webhook Bot Server")

# Carrega e inicializa o logger básico caso não tenha sido inicializado
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def query_prices_db(sql_query: str) -> str:
    """Executa consultas SQL de leitura (SELECT) no banco de dados SQLite local.
    Útil para recuperar informações de produtos, histórico de preços e descontos.

    Args:
        sql_query: Uma string de consulta SQL SELECT válida.
    """
    try:
        with sqlite3.connect(DEFAULT_DB) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(sql_query)
            rows = cursor.fetchall()
            return str([dict(row) for row in rows])
    except Exception as e:
        return f"Erro ao executar query: {str(e)}"


def run_store_scraper(store: str) -> str:
    """Executa a varredura (scraping) de preços em uma loja específica e atualiza o banco de dados.

    Args:
        store: A loja para varrer. Deve ser 'ous', 'netshoes' ou 'centauro'.
    """
    if store not in ("ous", "netshoes", "centauro"):
        return f"Erro: Loja desconhecida '{store}'. Escolha entre 'ous', 'netshoes' ou 'centauro'."
    try:
        args = argparse.Namespace(
            db=DEFAULT_DB,
            env=DEFAULT_ENV,
            sources=[store],
            mode="alert",
            digest_hours=24,
            no_telegram=True,  # não envia mensagem de alerta do monitor para evitar duplicidade
            dry_run_telegram=False,
        )
        status = cmd_run(args)
        if status == 0:
            return f"Sucesso! A loja '{store}' foi re-analisada e os preços no banco local foram atualizados."
        return f"Atenção: A varredura na loja '{store}' retornou código de status {status}."
    except Exception as e:
        return f"Falha na execução do scraper da loja '{store}': {str(e)}"


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
        if is_snapshot:
            args = argparse.Namespace(
                db=DEFAULT_DB,
                env=DEFAULT_ENV,
                sources=sources,
                no_telegram=False,
                dry_run_telegram=False,
            )
            status = cmd_snapshot(args)
        else:
            args = argparse.Namespace(
                db=DEFAULT_DB,
                env=DEFAULT_ENV,
                sources=sources,
                mode="alert",
                digest_hours=24,
                no_telegram=False,
                dry_run_telegram=False,
            )
            status = cmd_run(args)

        if status != 0:
            log.warning("Scraper finalizou com status de erro %s", status)
    except Exception as e:
        log.exception("Erro interno ao rodar tarefa de scraping")
        try:
            httpx.post(
                f"{API_BASE}/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"❌ <b>Erro interno na execução do scraper:</b>\n<pre>{str(e)}</pre>",
                    "parse_mode": "HTML",
                },
                timeout=10.0,
            )
        except Exception as msg_err:
            log.error("Não foi possível notificar erro ao usuário: %s", msg_err)


async def run_agy_agent_chat(user_message: str, bot_token: str, chat_id: str):
    """Inicializa e executa o agente de IA do Antigravity para interagir com o usuário."""
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        try:
            httpx.post(
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
        httpx.post(
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
            httpx.post(
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
            httpx.post(
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


@app.get("/setup-webhook")
def setup_webhook(url: str):
    """Auxiliar para configurar o webhook do Telegram.
    Ex: GET /setup-webhook?url=https://seu-app-coolify.com
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return {"error": "TELEGRAM_BOT_TOKEN não está definido nas variáveis de ambiente"}

    webhook_url = f"{url.rstrip('/')}/webhook"
    log.info("Configurando webhook do Telegram para: %s", webhook_url)

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{API_BASE}/bot{bot_token}/setWebhook",
            json={"url": webhook_url},
        )
    return resp.json()


def get_db_status_text() -> str:
    try:
        with sqlite3.connect(DEFAULT_DB) as conn:
            rows = conn.execute(
                "SELECT source, MAX(observed_at) FROM price_history GROUP BY source"
            ).fetchall()
            if not rows:
                return "<b>ℹ️ Nenhuma varredura registrada no banco de dados ainda.</b>"
            lines = ["<b>ℹ️ Status das Varreduras (Última Observação):</b>"]
            for source, observed_at in rows:
                dt = observed_at.replace("T", " ").split("+")[0]
                lines.append(f"• <code>{source}</code>: {dt}")
            return "\n".join(lines)
    except Exception as e:
        return f"❌ Erro ao consultar status das varreduras: <pre>{str(e)}</pre>"


def get_latest_db_promotions(source: str) -> list[dict]:
    with sqlite3.connect(DEFAULT_DB) as conn:
        conn.row_factory = sqlite3.Row
        if source == "netshoes":
            query_sources = ("netshoes", "netshoes_baw", "netshoes_adidas")
        else:
            query_sources = (source,)

        placeholders = ",".join("?" for _ in query_sources)
        rows = conn.execute(f"""
            WITH latest AS (
                SELECT source, sku, list_price, price, sizes, stock_qty, observed_at,
                       ROW_NUMBER() OVER (PARTITION BY source, sku
                                          ORDER BY observed_at DESC) AS rn
                  FROM price_history
                 WHERE source IN ({placeholders})
            )
            SELECT p.source, p.sku, p.name, p.url, p.image,
                   l.list_price, l.price, l.observed_at,
                   l.sizes, l.stock_qty,
                   NULL AS prev_price,
                   NULL AS prev_list_price,
                   NULL AS prev_observed_at
              FROM latest l
              JOIN products p USING (source, sku)
             WHERE l.rn = 1
               AND l.list_price IS NOT NULL
               AND l.list_price > l.price
             ORDER BY (1.0 - l.price / l.list_price) DESC, p.name
        """, query_sources).fetchall()
        return [dict(row) for row in rows]


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Recebe webhooks do Telegram para cliques de botões e mensagens do usuário."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        log.error("TELEGRAM_BOT_TOKEN não configurado no ambiente.")
        return JSONResponse({"status": "error", "message": "Bot token not configured"}, status_code=500)

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
        message_id = message["message_id"]

        # Responde ao Telegram imediatamente para remover o estado de carregamento do botão
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{API_BASE}/bot{bot_token}/answerCallbackQuery",
                json={"callback_query_id": callback_id},
                timeout=5.0,
            )

        # Helper para editar a mensagem/menu atual
        async def edit_menu(text: str, reply_markup: dict):
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{API_BASE}/bot{bot_token}/editMessageText",
                    json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "reply_markup": reply_markup,
                        "disable_web_page_preview": True,
                    },
                    timeout=5.0,
                )

        # Tratar navegação do menu interativo
        if callback_data.startswith("menu:"):
            target = callback_data.split(":")[1]
            if target == "main":
                await edit_menu("Escolha uma opção no menu abaixo para monitoramento:", MAIN_KEYBOARD)
            elif target == "db":
                await edit_menu("Filtros de consulta rápida de preços no banco de dados local (DB):", DB_KEYBOARD)
            elif target == "scrapers":
                await edit_menu("Selecione qual varredura de preços (scraper) ao vivo deseja rodar:", SCRAPERS_KEYBOARD)
            elif target == "status":
                status_text = get_db_status_text()
                back_keyboard = {"inline_keyboard": [[{"text": "🔙 Menu Principal", "callback_data": "menu:main"}]]}
                await edit_menu(status_text, back_keyboard)
            return JSONResponse({"status": "menu_updated"})

        # Tratar consultas diretas ao banco de dados (DB)
        if callback_data.startswith("db:"):
            source = callback_data.split(":")[1]
            source_labels = {
                "ous": "ÖUS",
                "netshoes": "Netshoes",
                "centauro": "Centauro"
            }
            label = source_labels.get(source, source.upper())
            promotions = get_latest_db_promotions(source)
            background_tasks.add_task(send_db_promotions, label, promotions, bot_token=bot_token, chat_id=chat_id)
            return JSONResponse({"status": "db_query_queued"})

        # Determina a ação para disparar o scraper
        sources = None
        is_snapshot = False

        if callback_data.startswith("run:"):
            action = callback_data.split(":")[1]
            if action != "all":
                sources = [action]
        elif callback_data == "run:snapshot":
            is_snapshot = True
        else:
            return JSONResponse({"status": "unknown_callback_data"})

        # Dispara o scraper em segundo plano
        background_tasks.add_task(run_scraper_task, sources, is_snapshot, bot_token, str(chat_id))
        return JSONResponse({"status": "task_queued"})

    # 2. Tratar mensagens normais do chat (ex: /start, /menu, ou mensagens de texto comuns)
    if "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        if not text:
            return JSONResponse({"status": "ignored"})

        if text.startswith("/start") or text.startswith("/menu"):
            send_menu_message(bot_token=bot_token, chat_id=chat_id)
            return JSONResponse({"status": "menu_sent"})

        # Qualquer outra mensagem de texto exibe o menu principal explicativo
        send_menu_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text="Para interagir com o monitor de preços, utilize os botões interativos abaixo:"
        )
        return JSONResponse({"status": "menu_sent"})

    return JSONResponse({"status": "ignored"})
