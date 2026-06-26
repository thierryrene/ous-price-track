# Plano de Ação: Deploy do OUS Price Monitor no Coolify (Com Agente de IA AGY)

Este documento descreve o plano de ação, o contexto de desenvolvimento e os passos necessários para realizar o deploy da aplicação **ous-price-monitor** no Coolify (Digital Ocean).

A aplicação conta com um **sistema híbrido**:
1. **Determinístico por Botões:** Cliques rápidos no Telegram que acionam os scrapers das lojas.
2. **Cognitivo por Chat (IA):** Código do agente existe, mas o webhook atual
   responde mensagens comuns com o menu para manter estabilidade. Reative a IA
   somente após validar a política de SQL read-only e rate limit.

---

## 📋 Contexto e Arquitetura

Migramos a infraestrutura para rodar em modo servidor na VPS da Digital Ocean gerenciada pelo Coolify para suportar webhooks do Telegram de forma estável.

### Componentes Integrados:
1. **Servidor API Webhook Híbrido (`src/ous_monitor/server.py`):** Servidor FastAPI que gerencia webhooks do Telegram. 
   * Mensagens de callback (clique nos botões inline) disparam tarefas tradicionais de scraping em segundo plano.
   * Mensagens de texto comum são repassadas ao **Agente AGY**, que usa inteligência artificial para ler e interagir.
2. **Agente de IA (AGY SDK):** Configurado para usar o modelo `gemini-1.5-flash` quando reativado. O agente possui duas ferramentas em Python:
   * `query_prices_db`: Executa queries SQL no banco SQLite local para extrair histórico de preços e insights.
   * `run_store_scraper`: Dispara o scraper de uma loja e atualiza a base de dados.
3. **Notificador (`src/ous_monitor/notifier.py`):** Anexa o teclado de menu do bot no final de cada resposta gerada pelo monitor ou pelo agente da IA, permitindo continuidade na interação.
4. **Dockerfile e Docker Compose:** Conteineriza o app usando a imagem do Playwright oficial, incluindo o SDK do Antigravity nas dependências.

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
* `WEBHOOK_ADMIN_TOKEN`: Token administrativo para `/setup-webhook` e `/status`.
* `TELEGRAM_WEBHOOK_SECRET`: Secret validado em todo POST recebido em `/webhook`.
* `TELEGRAM_ALLOWED_CHAT_IDS`: Lista opcional de chats permitidos; se vazio, usa `TELEGRAM_CHAT_ID`.
* `GEMINI_API_KEY`: Necessário apenas se a IA for reativada.
* `CENTAURO_PROXY` *(Opcional)*: URL do proxy se for usar para contornar o bloqueio do Akamai na Centauro.

### Passo 4: Build e Deploy
1. Clique em **Deploy** no topo direito do painel e aguarde a finalização da build do Playwright.
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

## 💬 Testando o Agente no Telegram

Com o webhook ativo e a `GEMINI_API_KEY` configurada no Coolify, abra a conversa com seu bot no Telegram e tente interagir das duas formas:

1. **Via Botões:** Clique em `🟧 OUS` ou `📊 Snapshot Geral` para rodar o scraping tradicional.
2. **Via Chat (IA, se reativada):** Envie mensagens de texto como:
   * *"Agy, busque o calçado ÖUS Imigrante e me diga qual loja tem o preço mais baixo hoje."*
   * *"Qual foi a maior queda de preço registrada nas últimas semanas no banco de dados?"*
   * *"Rode a varredura da Netshoes para mim, por favor."*
