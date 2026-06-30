# Contexto de Desenvolvimento e Deploy: OUS Price Monitor

Este documento registra o estado atual do projeto **ous-price-monitor** no servidor do Thierry, consolidando a arquitetura, as modificações efetuadas e as instruções necessárias para retomar o desenvolvimento ou manutenção no futuro.

---

## 📋 Resumo da Infraestrutura e Deploy

* **Plataforma de Deploy:** Containerizado via Docker Compose no próprio servidor (VPS Digital Ocean).
* **Proxy Reverso e SSL:** Gerenciado pelo **Traefik (coolify-proxy)** global através de rede compartilhada `coolify`.
* **Domínio Ativo:** `https://price-monitor.thierryrenematos.tec.br`
* **Status do Webhook:** Configurado e ativado com sucesso em ambiente de produção.

---

## 🛠️ Modificações Realizadas na Infraestrutura (Docker)

### 1. Imagem base enxuta (`python:3.12-slim`)
* **Histórico:** a base Ubuntu Noble (`mcr.microsoft.com/playwright/python:*-noble`) existia por dois motivos hoje removidos: o Playwright (scraper da Centauro) e o binário `localharness` do SDK `google-antigravity`, que exigia `glibc` ≥ 2.39.
* **Solução atual:** com a Centauro e o AGY/antigravity removidos, o [Dockerfile](file:///root/price-monitor-thierry/Dockerfile) usa `python:3.12-slim` (Debian). Todas as dependências restantes (httpx, selectolax, curl_cffi, fastapi, uvicorn) são Python puro / wheels manylinux — imagem ~5× menor, sem navegador.

### 2. Mapeamento do Banco de Dados Histórico (`prices.db`)
* **Problema:** O container estava utilizando um volume nomeado Docker isolado e vazio, perdendo as consultas rápidas do histórico de preços.
* **Solução:** Alteramos o volume no [docker-compose.yml](file:///root/price-monitor-thierry/docker-compose.yml) para um **bind-mount local** (`./data:/app/data`). O container agora lê e grava diretamente no arquivo físico de **12.3 MB** localizado no host, mantendo toda a inteligência e dados de monitoramentos passados.

### 3. Ajustes de Conflito no Coolify
* Removemos a diretiva `ports` que expunha a porta `8000` física no host (já ocupada pelo painel do Coolify).
* Substituímos por `expose: - "8000"` para delegação do tráfego ao Traefik de forma isolada.
* Removemos a propriedade estática `container_name` para evitar conflitos de recriação de containers durante deploys sem downtime.

---

## 🔑 Credenciais e Ambiente (.env)

As variáveis de ambiente foram recuperadas de forma segura do GitHub Actions via workflow temporário e persistidas em [.env](file:///root/price-monitor-thierry/.env):
* `TELEGRAM_BOT_TOKEN`
* `TELEGRAM_CHAT_ID`
* `WEBHOOK_ADMIN_TOKEN`
* `TELEGRAM_WEBHOOK_SECRET`
* `TELEGRAM_ALLOWED_CHAT_IDS` (opcional; se vazio, usa `TELEGRAM_CHAT_ID`)

---

## 🕹️ Evolução da Interface do Bot (Telegram)

A interface é **determinística por botões**: a lógica em [server.py](file:///root/price-monitor-thierry/src/ous_monitor/server.py) e [notifier.py](file:///root/price-monitor-thierry/src/ous_monitor/notifier.py) usa menu inline direto e valida webhook/chat quando as variáveis de segurança estão configuradas.

### 1. Menu Inline Principal
O `MENU_KEYBOARD` é gerado a partir do registry de fontes e permite rodar fontes individuais, `Rodar Todas`, `Snapshot Geral` e o menu de promoções das últimas 24h.

### 2. Chat de Texto
* Qualquer mensagem de texto comum (que não seja `/start` ou `/menu`) devolve a mensagem padrão para usar os botões interativos. O bot não tem chat com IA.

### 3. Rastreabilidade Operacional
O banco possui `runs` e `source_runs`, permitindo auditar última execução por fonte, contagens brutas/filtradas, falhas e `run_id`. Use:

```bash
PYTHONPATH=src python -m ous_monitor.cli status
curl "https://price-monitor.thierryrenematos.tec.br/status?token=$WEBHOOK_ADMIN_TOKEN"
```

---

## 🚀 Como Retomar o Desenvolvimento no Futuro

### Comandos de Operação Úteis
No diretório `/root/price-monitor-thierry`:
* **Atualizar Código e Subir Alterações:**
  ```bash
  docker compose up -d --build
  ```
* **Acompanhar os Logs da Aplicação/Scrapers:**
  ```bash
  docker logs -f ous-price-monitor
  ```
* **Forçar Reconfiguração do Webhook:**
  ```bash
  curl -s "https://price-monitor.thierryrenematos.tec.br/setup-webhook?url=https://price-monitor.thierryrenematos.tec.br&token=$WEBHOOK_ADMIN_TOKEN"
  ```
