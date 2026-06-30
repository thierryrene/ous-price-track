# Plano de Ação: Deploy do OUS Price Monitor no Coolify (Com Agente de IA AGY)

Este documento descreve o plano de ação, o contexto de desenvolvimento e os passos necessários para realizar o deploy da aplicação **ous-price-monitor** no Coolify (Digital Ocean).

A aplicação conta com um **sistema híbrido operacional**:
1. **GitHub Actions:** agenda diária do monitor e persistência do `data/prices.db`.
2. **Coolify/FastAPI:** servidor do bot Telegram para webhooks e ações on-demand por botões.

O código contém a integração AGY/Gemini, mas o webhook atual responde mensagens
de texto comum com o **menu** (IA desativada no caminho do webhook). A consulta
SQL do agente já usa modo read-only; reative a IA somente após validar também o
rate limit.

---

## 📋 Contexto e Arquitetura

Migramos a infraestrutura para rodar em modo servidor na VPS da Digital Ocean gerenciada pelo Coolify para suportar webhooks do Telegram de forma estável.

### Componentes Integrados:
1. **Servidor API Webhook Híbrido (`src/ous_monitor/server.py`):** Servidor FastAPI que gerencia webhooks do Telegram. 
   * Mensagens de callback (clique nos botões inline) disparam tarefas tradicionais de scraping em segundo plano.
   * Mensagens de texto comum retornam o menu do monitor (IA desativada no webhook).
2. **Agente de IA (AGY SDK):** Configurado para usar o modelo `gemini-1.5-flash` quando reativado. O agente possui duas ferramentas em Python:
   * `query_prices_db`: Executa queries SQL no banco SQLite local para extrair histórico de preços e insights.
   * `run_store_scraper`: Dispara o scraper de uma loja e atualiza a base de dados.
3. **Notificador (`src/ous_monitor/notifier.py`):** Anexa o teclado de menu do bot no final de cada resposta gerada pelo monitor ou pelo agente da IA, permitindo continuidade na interação.
4. **Dockerfile e Docker Compose:** Conteineriza o app sobre a base Ubuntu Noble (`mcr.microsoft.com/playwright/python:*-noble`), escolhida pelo glibc 2.39 que o SDK do Antigravity exige.

---

## 🚀 Guia de Deploy Passo a Passo

### Passo 1: Configuração do Recurso no Coolify
1. No painel do **Coolify**, crie um novo recurso apontando para o seu repositório Git `thierryrene/ous-price-track` (branch `main`).
2. Selecione o formato de build **Docker Compose**. O Coolify lerá o arquivo `docker-compose.yml` automaticamente.

### Passo 2: Definição do Domínio e SSL
1. No campo **Domains** da aplicação no Coolify, defina o subdomínio que o webhook do Telegram irá apontar.
   * Exemplo: `https://ous-bot.seu-dominio.com`
2. O Coolify cuidará do SSL/HTTPS automaticamente.

### Passo 3: Variáveis de Ambiente
Na aba **Environment Variables** do Coolify, adicione:
* `TELEGRAM_BOT_TOKEN`: O token fornecido pelo `@BotFather`.
* `TELEGRAM_CHAT_ID`: O ID do chat onde os alertas devem ser entregues.
<<<<<<< HEAD
* `TELEGRAM_WEBHOOK_SECRET`: segredo enviado no `setWebhook` e validado pelo servidor.
* `TELEGRAM_ALLOWED_CHAT_IDS`: lista de chats autorizados, separada por vírgula.
* `WEBHOOK_ADMIN_TOKEN`: Token administrativo para `/setup-webhook` e `/status` (nome legado `ADMIN_TOKEN` também aceito).
* `GEMINI_API_KEY` *(Opcional)*: chave do Gemini API, necessária apenas se a IA AGY for reativada.

### Passo 4: Build e Deploy
1. Clique em **Deploy** no topo direito do painel e aguarde a finalização da build.
2. Garanta que o status ficou em verde (**Running**).

### Passo 5: Ativação do Webhook do Telegram
Uma vez que a aplicação esteja ativa na internet:
1. Acesse no seu navegador a URL de setup passando o seu domínio:
   `https://ous-bot.seu-dominio.com/setup-webhook?url=https://ous-bot.seu-dominio.com&token=SEU_WEBHOOK_ADMIN_TOKEN`
2. Você deverá ver o retorno confirmando o sucesso:
   ```json
   {"ok": true, "result": true, "description": "Webhook was set"}
   ```

---

## 💬 Testando no Telegram

Com o webhook ativo, abra a conversa com seu bot no Telegram e teste:

1. Envie `/start` ou `/menu`.
2. Clique em `🟧 OUS`, `⚽ Umbro`, `📊 Snapshot Geral` ou nas opções de catálogo.
3. **Via Chat (IA, se reativada):** mensagens de texto hoje retornam o menu; com a
   IA reativada, frases como *"Qual foi a maior queda de preço nas últimas semanas?"*
   seriam respondidas pelo agente AGY.
