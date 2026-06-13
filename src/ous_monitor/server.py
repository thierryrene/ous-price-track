from __future__ import annotations

import argparse
import logging
import os
import threading
from pathlib import Path
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from .cli import cmd_run, cmd_snapshot, DEFAULT_DB, DEFAULT_ENV
from .notifier import send_menu_message, API_BASE

log = logging.getLogger("ous_monitor.server")

app = FastAPI(title="OUS Price Monitor Webhook Bot Server")

# Carrega e inicializa o logger básico caso não tenha sido inicializado
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


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
            # Se status != 0, houve falha em alguma fonte mas pode ter trazido produtos das outras.
            # O próprio cmd_run notifica se houver produtos, mas caso não tenha retornado nada:
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

        if callback_data.startswith("run:"):
            action = callback_data.split(":")[1]
            if action != "all":
                sources = [action]
        elif callback_data == "run:snapshot":
            is_snapshot = True
        else:
            return JSONResponse({"status": "unknown_callback_data"})

        # Dispara o scraper em segundo plano (background tasks do FastAPI/thread pool)
        background_tasks.add_task(run_scraper_task, sources, is_snapshot, bot_token, str(chat_id))
        return JSONResponse({"status": "task_queued"})

    # 2. Tratar mensagens normais do chat (ex: /start, /menu)
    if "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        if text.startswith("/start") or text.startswith("/menu"):
            send_menu_message(bot_token=bot_token, chat_id=chat_id)
            return JSONResponse({"status": "menu_sent"})

    return JSONResponse({"status": "ignored"})
