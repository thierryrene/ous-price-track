# Plano de Ação: Deploy do OUS Price Monitor no Coolify

Este documento descreve o plano de ação, o contexto de desenvolvimento e os passos necessários para realizar o deploy da aplicação **ous-price-monitor** no Coolify (Digital Ocean).

A aplicação conta com um **sistema híbrido operacional**:
1. **GitHub Actions:** agenda diária do monitor e persistência do `data/prices.db`.
2. **Coolify/FastAPI:** servidor do bot Telegram para webhooks e ações on-demand por botões.

O bot é **orientado a botões** (menus inline); mensagens de texto comum recebem o menu.

---

## 📋 Contexto e Arquitetura

Migramos a infraestrutura para rodar em modo servidor na VPS da Digital Ocean gerenciada pelo Coolify para suportar webhooks do Telegram de forma estável.

### Componentes Integrados:
1. **Servidor API Webhook (`src/ous_monitor/server.py`):** Servidor FastAPI que gerencia webhooks do Telegram.
   * Mensagens de callback (clique nos botões inline) disparam tarefas de scraping em segundo plano.
   * Mensagens de texto comum retornam o menu do monitor.
2. **Notificador (`src/ous_monitor/notifier.py`):** Anexa o teclado de menu do bot no final de cada resposta, permitindo continuidade na interação.
3. **Dockerfile e Docker Compose:** Conteineriza o app sobre `python:3.12-slim` (Debian). Todas as dependências são Python puro / wheels manylinux — sem navegador.

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
* `TELEGRAM_WEBHOOK_SECRET`: segredo enviado no `setWebhook` e validado pelo servidor.
* `TELEGRAM_ALLOWED_CHAT_IDS`: lista de chats autorizados, separada por vírgula.
* `WEBHOOK_ADMIN_TOKEN`: Token administrativo para `/setup-webhook` e `/status` (nome legado `ADMIN_TOKEN` também aceito).

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
