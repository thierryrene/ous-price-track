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

### 1. Atualização do SO Base para Solução de Link Dinâmico (glibc)
* **Problema:** O binário `localharness` (do SDK `google-antigravity`) necessitava de uma versão do `glibc` com suporte a `GLIBC_ABI_DT_RELR`, que não estava disponível no Ubuntu 22.04 (Jammy).
* **Solução:** Alteramos a imagem base no [Dockerfile](file:///root/price-monitor-thierry/Dockerfile) de `v1.48.0-jammy` para `v1.48.0-noble` (Ubuntu 24.04). A nova versão possui o `glibc` 2.39 e resolveu o erro de execução em testes no Telegram.

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
* `GEMINI_API_KEY` (configurada com a chave ativa fornecida pelo usuário)

---

## 🕹️ Evolução da Interface do Bot (Telegram)

Para focar na **interface determinística por botões** e deixar a IA em segundo plano, reestruturamos a lógica em [server.py](file:///root/price-monitor-thierry/src/ous_monitor/server.py) e [notifier.py](file:///root/price-monitor-thierry/src/ous_monitor/notifier.py):

### 1. Menus Inline Aninhados (Navegação GUI)
As telas se atualizam no mesmo balão de texto por meio de `editMessageText`, evitando poluição visual:
* **Menu Principal (`MAIN_KEYBOARD`):**
  * `🔍 Consultar DB` -> Encaminha para consulta de promoções salvas.
  * `⚡ Rodar Scrapers` -> Encaminha para gatilhos de varredura ao vivo.
  * `📊 Snapshot Geral` -> Executa o snapshot imediato de todas as lojas.
  * `ℹ️ Status Varreduras` -> Exibe as últimas execuções de cada marca no DB.
* **Menu do Banco de Dados (`DB_KEYBOARD`):**
  * `🟧 ÖUS`, `🟦 Netshoes` e `🟥 Centauro` que realizam queries SQLite imediatas e retornam as ofertas ativas no chat.
  * *Melhoria:* O botão da Netshoes agrupa automaticamente `netshoes`, `netshoes_baw` e `netshoes_adidas`.
* **Menu de Scrapers (`SCRAPERS_KEYBOARD`):**
  * Gatilhos individuais ou coletivos (`🔄 Rodar Todas`) para rodar os scrapers do Playwright em background.

### 2. Bypass de IA no Chat de Texto
* Qualquer mensagem de texto comum enviada ao bot (que não seja `/start` ou `/menu`) agora devolve a mensagem padrão para utilizar os botões interativos, ignorando a execução da IA/Gemini para economizar tokens e garantir estabilidade.

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
  curl -s https://price-monitor.thierryrenematos.tec.br/setup-webhook?url=https://price-monitor.thierryrenematos.tec.br
  ```

### Como Reativar a IA (Agy) se desejar futuramente:
1. No arquivo [server.py](file:///root/price-monitor-thierry/src/ous_monitor/server.py), vá até a seção de tratamento de mensagens de texto comuns (linhas ~313-320).
2. Substitua o envio do menu padrão pelo agendamento da tarefa da IA:
   ```python
   # Exemplo de reativação da IA:
   background_tasks.add_task(run_agy_agent_chat, text, bot_token, str(chat_id))
   ```
