# Changelog

Todas as mudanças notáveis deste projeto são documentadas aqui.
Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
o projeto ainda é `0.1.0` (sem releases tagueados), então as mudanças recentes
ficam em **[Não lançado]**.

## [Não lançado] — 2026-09-02

### Corrigido em 2026-09-24
- Monitoramento agendado transferido integralmente do GitHub Actions para o
  servidor, eliminando a divergência entre o banco remoto e o catálogo do bot.
- Duas coletas UTC persistentes (`12:alert` e `21:digest`) no processo FastAPI,
  com deduplicação de slots após reinícios e uso do mesmo lock das ações manuais.
- Alertas e digests exibem na primeira linha a data e hora da coleta em BRT.

### Adicionado
- **Acompanhamentos pessoais no Telegram**: filtros salvos (até 10 por chat),
  favoritos paginados, referências curtas de produto e preferências opt-in de
  alerta por horário UTC, com deduplicação persistente de cada evento entregue.
- **Contagens nos filtros**: categoria, teto de preço e desconto mínimo mostram
  quantas promoções do último snapshot bem-sucedido correspondem a cada opção.
- **Resumos personalizados automáticos**: filtros salvos acompanham novas
  promoções/quedas e favoritos acompanham qualquer mudança detectada; o laço do
  servidor entrega no horário configurado sem repetir notificações.
- **Navegação Telegram em uma única mensagem**: menus, filtros, resultados,
  paginação, status, manutenção e progresso agora reutilizam a mensagem do
  callback com `editMessageText`; páginas foram ajustadas para 5 produtos para
  permanecerem dentro do limite do Telegram.
- **Callbacks versionados e retrocompatíveis** (`bot/callbacks.py`), com
  validação rígida do limite de 64 bytes, e sessões de filtro persistidas na
  nova tabela SQLite `bot_sessions`.
- **Notificações históricas preservadas**: alertas e digests usam um botão
  dedicado **Abrir menu**, que cria uma nova tela sem substituir o alerta.
- **Catálogo rápido e verificável no Telegram**: consultas filtradas usam o
  último snapshot bem-sucedido sem scraping implícito, exibem frescor e paginam
  5 ofertas; snapshots vencidos são entregues e revalidados em segundo plano,
  e atualizações explícitas mostram progresso e reaplicam os filtros.
- `products.last_seen_run_id`, metadados `SourceFreshness` e migração/backfill
  para excluir ofertas indisponíveis ou ausentes da coleta confirmada mais recente.
- Paralelismo limitado entre domínios (`SCRAPE_MAX_WORKERS`), mantendo as
  fontes Netshoes sequenciais para respeitar rate-limit.
- **Autocuidado automático do SQLite**: no servidor, manutenção diária com
  backup consistente, retenção de 7 cópias, limpeza de histórico/runs e
  `VACUUM`; no GitHub Actions, workflow semanal com artifact do backup por
  14 dias. O limite padrão é 50 MB e gera alerta no Telegram se persistir.
- Subcomando `maintain` (dry-run por padrão; `--apply` executa backup,
  normalização segura e compactação).
- **Modo resumo de alta carga** no Telegram (`notifier.build_summary` +
  `categories.py`): uma linha por item agrupada por tipo de peça, com cap por
  grupo. `send_alert` resume acima de `SUMMARY_THRESHOLD` (default 15);
  `send_digest` resume por padrão. Envs `SUMMARY_THRESHOLD`/`SUMMARY_PER_GROUP`.
- **Camada de serviços** (`services.py`): `MonitorService` (orquestração com
  lock de arquivo `fcntl` + run-tracking), `CatalogService` (catálogo, `purge`,
  `normalize`, stats) e `SourceRegistry`.
- **Run-tracking** no storage: tabelas `runs`/`source_runs`, coluna `run_id`,
  funções `start_run`/`finish_run`/`record_source_run`/`latest_source_runs`;
  subcomando CLI `status` e endpoint `/status`.
- **Fontes** `umbro` (outlet VTEX), `converse` (Sale Magento 2 com tamanhos
  saláveis por cor) e `approve` (Tiendanube, só on-demand).
- **Backoff** exponencial no scraper Netshoes para HTTP 429/503 (respeita
  `Retry-After`) — `scrapers/netshoes.py:_get_with_retry`.
- **Suíte de testes** em `tests/` (storage, sources, filters, summary, netshoes
  retry) — rode `python -m unittest discover -s tests`.
- Endpoints `/health/ready` e endurecimento de segurança do webhook
  (`WEBHOOK_ADMIN_TOKEN`, validação https no `setup-webhook`, SQL read-only).
- Dashboard HTML (`html_generator.py`) regenerado a cada run.
- **`AGENTS.md`** como fonte única de orientação para agentes; `CLAUDE.md`,
  `MIMO.md`, `GEMINI.md` e `.github/copilot-instructions.md` viram ponteiros.

### Alterado
- Webhook reutiliza clientes HTTP, confirma callbacks rapidamente e desloca
  adaptadores síncronos para o executor; atualizações duplicadas são rejeitadas.
- Menu do bot separa consulta, atualização e administração; listagens comuns
  não usam mais o marcador incorreto `🆕`.
- Menu do Telegram reorganizado em atalhos e submenus. O catálogo por loja e
  seus labels agora são derivados de `sources.SOURCES`, cobrindo
  automaticamente as 9 fontes (incluindo Converse e Netshoes BaW).
- Promoções do dia agora usam a categorização canônica e oferecem também
  camisas de time e agasalhos. O status lista inclusive fontes ainda sem dados,
  e varreduras sem mudanças enviam confirmação de conclusão.
- **Base Docker** de `mcr.microsoft.com/playwright/python:*-noble` para
  **`python:3.12-slim`** (imagem ~5× menor; deps são Python puro / wheels
  manylinux, sem navegador).
- **Registro de fontes unificado** em `sources.py` (`SOURCES`/`SourceConfig`),
  consumido por CLI, bot, server e dashboard.
- Storage com hardening: WAL, `busy_timeout`, `foreign_keys`, dedup de SKU.
- O histórico de preços passa a gravar somente produtos novos ou observações
  realmente alteradas. Produtos inalterados continuam atualizando `last_seen`,
  evitando que o SQLite cresça milhares de linhas por execução.
- O filtro de tamanho agora é estrito: calçados exigem 42/43; roupas exigem
  M/G/GG; acessórios continuam independentes de grade.

### Removido
- **Fonte Centauro** (e o scraper Playwright) — bloqueio agressivo do Akamai a
  tornava inviável; nenhuma fonte restante usa navegador.
- **Agente de IA "AGY"** (`google-antigravity` + Gemini): era código morto (o
  webhook nunca o chamava). Removidos `run_agy_agent_chat`, `query_prices_db`,
  `run_store_scraper`, a dependência e as envs `GEMINI_API_KEY`.

### Corrigido
- Revalidação automática de catálogo vencido recebia argumentos duplicados no
  background task e podia falhar antes de iniciar o scraper.
- Repetir um filtro já selecionado podia transformar o erro inofensivo
  `message is not modified` em uma nova mensagem duplicada.
- Retenção de histórico agora preserva sempre a observação mais recente de cada
  SKU, mesmo quando ela própria tem mais de 90 dias, mantendo a referência para
  futuras detecções de mudança.
- Logs `INFO` de `httpx/httpcore` foram silenciados no servidor para impedir que
  URLs da API do Telegram revelem o token do bot.
- Allowlist do webhook agora usa `TELEGRAM_CHAT_ID` como fallback quando
  `TELEGRAM_ALLOWED_CHAT_IDS` está vazio e rejeita callbacks de fontes/filtros
  desconhecidos.
- Crescimento contínuo de `data/prices.db` acima do limite de 100 MiB do
  GitHub, que impedia o workflow de persistir snapshots e concluir com sucesso.
- Falhas do Netshoes em produção por rate-limit (429) — agora com backoff.
- `approve` marcada `run_in_ci=False` (registro condizente com o cron).
- Marcador de conflito órfão (`<<<<<<< HEAD`) que vazou para o `main` num merge.

## Histórico anterior

Antes deste changelog, o histórico vive nos commits do Git. Marcos relevantes:
`feat: add umbro and harden monitor operations` (`fa076fe`),
`feat: add Adidas Originals source, fix bugs, and improve Telegram bot`
(`e93e22a`). Snapshots diários do `data/prices.db` são commits `chore(data):`
gerados pelo GitHub Actions.
