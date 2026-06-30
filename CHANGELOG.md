# Changelog

Todas as mudanças notáveis deste projeto são documentadas aqui.
Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
o projeto ainda é `0.1.0` (sem releases tagueados), então as mudanças recentes
ficam em **[Não lançado]**.

## [Não lançado] — 2026-06-30

### Adicionado
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
- **Fontes** `umbro` (outlet VTEX) e `approve` (Tiendanube, só on-demand).
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
- **Base Docker** de `mcr.microsoft.com/playwright/python:*-noble` para
  **`python:3.12-slim`** (imagem ~5× menor; deps são Python puro / wheels
  manylinux, sem navegador).
- **Registro de fontes unificado** em `sources.py` (`SOURCES`/`SourceConfig`),
  consumido por CLI, bot, server e dashboard.
- Storage com hardening: WAL, `busy_timeout`, `foreign_keys`, dedup de SKU.

### Removido
- **Fonte Centauro** (e o scraper Playwright) — bloqueio agressivo do Akamai a
  tornava inviável; nenhuma fonte restante usa navegador.
- **Agente de IA "AGY"** (`google-antigravity` + Gemini): era código morto (o
  webhook nunca o chamava). Removidos `run_agy_agent_chat`, `query_prices_db`,
  `run_store_scraper`, a dependência e as envs `GEMINI_API_KEY`.

### Corrigido
- Falhas do Netshoes em produção por rate-limit (429) — agora com backoff.
- `approve` marcada `run_in_ci=False` (registro condizente com o cron).
- Marcador de conflito órfão (`<<<<<<< HEAD`) que vazou para o `main` num merge.

## Histórico anterior

Antes deste changelog, o histórico vive nos commits do Git. Marcos relevantes:
`feat: add umbro and harden monitor operations` (`fa076fe`),
`feat: add Adidas Originals source, fix bugs, and improve Telegram bot`
(`e93e22a`). Snapshots diários do `data/prices.db` são commits `chore(data):`
gerados pelo GitHub Actions.
