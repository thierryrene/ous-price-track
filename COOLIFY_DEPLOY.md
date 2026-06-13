# Plano de Ação: Deploy do OUS Price Monitor no Coolify

Este documento descreve o plano de ação, o contexto de desenvolvimento e os passos necessários para realizar o deploy da aplicação **ous-price-monitor** no Coolify (Digital Ocean) com suporte a atualizações sob demanda via Telegram.

---

## 📋 Contexto e Arquitetura

O projeto original foi desenhado para rodar via cronjobs locais em uma máquina física. Para permitir o acionamento de consultas **on-demand** (ex: o usuário clica em um botão no chat e o bot executa o scraper imediatamente e responde com os dados mais recentes), migramos a infraestrutura para rodar em modo servidor na VPS da Digital Ocean gerenciada pelo Coolify.

### Componentes Atualizados:
1. **Servidor API Webhook (`src/ous_monitor/server.py`):** Servidor construído em FastAPI que recebe os comandos e interações do Telegram, dispara os scrapers em segundo plano (`BackgroundTasks`) de forma não-bloqueante, e responde ao usuário de forma síncrona.
2. **Notificador (`src/ous_monitor/notifier.py`):** Modificado para acoplar botões inline ao final das mensagens de relatório, oferecendo gatilhos rápidos ao usuário.
3. **Dockerfile e Docker Compose:** Permite a conteinerização da aplicação usando a imagem oficial do Playwright, resolvendo problemas de dependência de navegadores para o scraper da Centauro.
4. **Volume do Banco de Dados SQLite:** Persistência garantida para manter o histórico acumulado no `prices.db`.

---

## 🚀 Guia de Deploy Passo a Passo

### Passo 1: Configuração do Recurso no Coolify
1. Acesse o painel do seu **Coolify**.
2. Vá até o seu **Projeto** e **Ambiente** (ex: Production).
3. Clique em **+ New Resource** e selecione **GitHub Repository** (conete a sua conta do GitHub se necessário).
4. Selecione o repositório `thierryrene/ous-price-track` e a branch `main`.

### Passo 2: Configuração da Build
1. Ao carregar o projeto, selecione o formato **Docker Compose** como o tipo de build da aplicação.
2. O Coolify lerá automaticamente o arquivo `docker-compose.yml` da raiz do repositório.

### Passo 3: Definição do Domínio e SSL
1. No campo **Domains** da aplicação no Coolify, defina o subdomínio que o webhook do Telegram irá apontar.
   * Exemplo: `https://ous-bot.seu-dominio.com`
2. O Coolify criará automaticamente as rotas de proxy reverso e os certificados de SSL/HTTPS (obrigatórios para o Telegram funcionar).

### Passo 4: Variáveis de Ambiente
Na aba **Environment Variables** do Coolify, adicione as credenciais essenciais para o funcionamento do bot (equivalente ao seu `.env` local):
* `TELEGRAM_BOT_TOKEN`: O token fornecido pelo `@BotFather`.
* `TELEGRAM_CHAT_ID`: O ID do chat onde os alertas e relatórios devem ser entregues.
* `CENTAURO_PROXY` *(Opcional)*: URL do proxy (ex: `socks5://host:port` ou `http://user:pass@host:port`) se for utilizar para contornar o bloqueio do Akamai na Centauro.

### Passo 5: Build e Deploy
1. Clique em **Deploy** no topo direito do painel.
2. Acompanhe os logs. A primeira build baixa a imagem pesada do Playwright, mas as próximas serão muito mais rápidas devido ao cache do Docker.
3. Certifique-se de que o container subiu e o status ficou em verde (**Running**).

### Passo 6: Ativação do Webhook do Telegram
Uma vez que a aplicação esteja ativa na internet:
1. Abra seu navegador e acesse o endpoint `/setup-webhook` fornecendo o seu domínio configurado no Passo 3.
   * URL de exemplo:
     `https://ous-bot.seu-dominio.com/setup-webhook?url=https://ous-bot.seu-dominio.com`
2. Você deverá ver o seguinte retorno do Telegram confirmando o sucesso:
   ```json
   {"ok": true, "result": true, "description": "Webhook was set"}
   ```

---

## 🛠️ Manutenção e Logs

* **Visualizar logs de execução:** No painel do Coolify, selecione a aplicação e vá na aba **Logs** para ver em tempo real as consultas sendo feitas nas lojas quando você clica em um botão do Telegram.
* **Persistência do Banco:** O volume `ous_monitor_data` estará localizado no host em `/var/lib/docker/volumes/ous_monitor_data/_data`. O banco SQLite `prices.db` ficará seguro lá e não sofrerá perda de dados em novos deploys.
