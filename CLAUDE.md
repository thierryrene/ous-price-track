# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Daily monitor de marcas de streetwear/calçado: **ÖUS**, **BaW Clothing**,
**Adidas** e **Umbro**. Scrapes cada marca da loja própria (quando aplicável)
+ retailers marketplace, stores price history em SQLite, reports changes, and
serves Telegram/FastAPI + an HTML dashboard.

Active sources are defined once in `src/ous_monitor/sources.py` and consumed by
CLI, Telegram labels/buttons, server validation and dashboard config:

- `ous` — ous.com.br/garimpo (outlet oficial ÖUS, VTEX)
- `umbro` — umbro.com.br/outlet (outlet oficial Umbro, VTEX coleção 921)
- `netshoes` — clube.netshoes.com.br filtrado por marca ÖUS
- `baw` — bawclothing.com.br (catálogo completo BaW, plataforma Wake/FBits)
- `netshoes_baw` — clube.netshoes.com.br filtrado por marca BaW Clothing
- `netshoes_adidas` — clube.netshoes.com.br filtrado por marca Adidas (~6900 itens,
  ~164 páginas, ~4min de scraping; NÃO inclui Adidas Originals)
- `netshoes_adidas_originals` — clube.netshoes.com.br filtrado por marca Adidas
  Originals (~92 itens, ~4 páginas; marca separada na Netshoes: `marca=adidas-originals`)

Cada par (loja, marca) é uma source distinta — não há coluna `brand` no DB,
quem segrega marca dentro do mesmo retailer é o `source_name`. `NetshoesScraper`
é parametrizado por marca; `NetshoesBawScraper` e `NetshoesAdidasOriginalsScraper`
são factories que devolvem instâncias configuradas para cada marca.

## Commands

All commands assume the venv is active: `source .venv/bin/activate`.

```bash
# Run all scrapers, persist, print new promotions detected this run
PYTHONPATH=src python -m ous_monitor.cli run

# Single source (useful while iterating on one scraper)
PYTHONPATH=src python -m ous_monitor.cli run --sources ous

# Historical report from the DB (no network)
PYTHONPATH=src python -m ous_monitor.cli report --days 7

# Snapshot of products currently on sale (latest observation per SKU)
PYTHONPATH=src python -m ous_monitor.cli list --limit 50
```

There is no test suite yet. To validate a scraper change, run it standalone:

```bash
PYTHONPATH=src python -c "
from ous_monitor.scrapers.baw import BawScraper
ps = BawScraper().fetch_all()
print(len(ps), 'products,', sum(1 for p in ps if p.has_discount), 'on sale')
"
```

## Architecture

Main layers, all under `src/ous_monitor/`:

1. **Scrapers** (`scrapers/{ous,netshoes,baw,umbro,approve}.py`) — each implements
   the `Scraper` protocol from `scrapers/base.py`: a `source` string and a
   `fetch_all() -> list[Product]` method. Each scraper is responsible for its
   own pagination and **must walk all pages** (not just the first viewport's
   worth — see "Pagination contract" below).

2. **Storage** (`storage.py`) — SQLite with `products`, `price_history`,
   `runs`, and `source_runs`. `record_run()` upserts product rows, appends a
   price observation linked to `run_id`, deduplicates repeated SKUs, and
   returns counters. `find_changes()` uses `LAG` window functions to detect 4
   mutually-exclusive categories of change since a cutoff timestamp:
   `new_promo` (started/deepened a discount), `ended` (back to list price),
   `weaker` (still discounted but discount % shrunk by ≥25% relative — see
   `DISCOUNT_SHRINK_RATIO`), and `price_up` (price rose ≥5% — see
   `PRICE_UP_RATIO`). Priority: new_promo > ended > weaker > price_up. The
   thin wrapper `find_new_promotions()` is kept for backwards compat.

3. **CLI** (`cli.py`) — orchestrates: takes a file lock, runs each requested scraper in isolation
   (one source crashing does not stop the others), passes products to storage,
   queries `find_changes()`, and dispatches to the notifier in one of two
   modes (flag `--mode`):
   - `alert` (default): cutoff is `now - 10s`, notifier sends `send_alert()`
     — single-block message with all 4 categories interleaved.
   - `digest`: cutoff is `now - --digest-hours` (default 24h), notifier sends
     `send_digest()` — 4 separate sections with totals.
   GitHub Actions runs `alert` at 12h UTC and `digest` at 21h UTC (see
   workflow's `pick_mode` step).

The `Product` dataclass in `models.py` is the contract between scrapers and
storage. Add a field there if a new piece of data needs to flow through.

`server.py` exposes `/health`, `/health/ready`, `/status` and `/webhook`.
Production webhook setup requires `WEBHOOK_ADMIN_TOKEN`; Telegram webhook
requests can be protected with `TELEGRAM_WEBHOOK_SECRET`, and chat access is
restricted by `TELEGRAM_ALLOWED_CHAT_IDS` or `TELEGRAM_CHAT_ID`.

## Pagination contract

**Every scraper must paginate fully.** This is a hard requirement, not a nice-to-have. Each scraper logs the server-declared total on the first page and continues fetching until the source signals exhaustion. Concretely:

- **OUS** (`scrapers/ous.py`): VTEX returns `206 Partial Content` and a `resources: X-Y/TOTAL` header. Loop `_from`/`_to` by 50 until `start >= total`. ~3 pages.
- **Netshoes** (`scrapers/netshoes.py`): `__INITIAL_STATE__.SearchPage.totalPages` indicates total pages; loop `?page=N` until reached. Produtos ficam em `SearchPage.parentSkus` (não `products`). ~5 pages (ÖUS), ~2 pages (BaW Clothing), ~164 pages (Adidas — bem mais lento, ~4min com o delay de 1.5s).
- **BaW** (`scrapers/baw.py`): JSON-LD `ItemList.numberOfItems` declara o total; loop `?pagina=N&tamanho=24` até cobrir o total. **Pegadinha crítica**: passar apenas `?pagina=N` é silenciosamente ignorado e devolve sempre a pg 1 — só com `tamanho` junto a paginação destrava. ~25 páginas.

If you change a scraper, verify the log line `"<source>: total declarado = ..."` matches the count of products actually returned.

## Site-specific gotchas

- **OUS**: `Discount` field in the JSON is always `null` — derive promotion from `ListPrice > Price`. Color variants are separate `productId`s. Size variants live inside `items[]` and share pricing, so `items[0]` is enough.
- **Netshoes**: prices are integers in **cents** — divide by 100. Brand strings: ÖUS é `"ÖUS"` com umlaut (não `"OUS"`); BaW vem como `"BAW Clothing"` (slug `marca=baw-clothing`; `marca=baw` puro é outra marca genérica de 2 itens); Adidas é `"Adidas"` (slug `marca=adidas`; `marca=adidas-originals` é catalogada à parte com brand `"Adidas Originals"`, NÃO incluído em `netshoes_adidas`). Pagination is `?page=N`; `?p=N` is silently ignored. Search without `marca=...` brings non-target marketplace items.
- **Netshoes**: rate-limita (429) IPs compartilhados. O scraper repete com backoff exponencial (respeitando `Retry-After`) antes de desistir — ver `scrapers/netshoes.py:_get_with_retry`.
- **BaW** (`scrapers/baw.py`): plataforma Wake/FBits (não VTEX — APIs `catalog_system` retornam 404). Dois blocos JSON SSR são combinados por `item_id` (sufixo numérico no slug da URL): JSON-LD `ItemList` traz url/imagem/nome/preço corrente, e o JS inline `{item_list_name:"Hotsite products"…}` traz o `discount`. **ATENÇÃO**: `discount` no dataLayer é o **valor absoluto em reais economizado**, NÃO o percentual — logo `list_price = price + discount` (interpretar como % daria preços absurdos tipo "camiseta de R$ 690"). NÃO pedir `br` em `Accept-Encoding`: httpx só decodifica brotli com o pacote opcional `brotli`/`brotlicffi` e o servidor BaW sempre escolhe br quando oferecido — restrito a `gzip, deflate`. Paginação exige `?pagina=N&tamanho=24` juntos (ver pagination contract acima).

## Storage location

The default DB path is `<repo>/data/prices.db`. The `data/` directory is gitignored. For ad-hoc inspection: `sqlite3 data/prices.db ".schema"`.

## Filtros de ingestão

Vivem em [src/ous_monitor/filters.py](src/ous_monitor/filters.py) e são aplicados em `cli._scrape_and_persist` antes de `record_run`. Produtos rejeitados nunca entram no DB. **O DB é a fonte da verdade** — `notifier.py` e os subcomandos `list`/`report` confiam que o que está lá já passou pelo filtro.

Dois critérios encadeados (gênero antes de tamanho, pra que o motivo do log seja determinístico):

1. **Gênero/idade** (`gender.is_male_or_unisex`) — rejeita qualquer item cujo nome contém token feminino-exclusivo (`feminino`, `mulher`, `women`, `wmn`…), infantil/juvenil (`infantil`, `kids`, `junior`, `menina`, `bebe`, `baby`…), de maternidade, ou categoria feminina exclusiva (`calcinha`, `biquini`, `vestido`, `saia`…). Quando não há marcador algum, aceita (interpretação unissex). Vocabulário em [src/ous_monitor/gender.py](src/ous_monitor/gender.py).
2. **Tamanho 42/43** (`passes_size_filter`) — só atua em itens cujo nome contém `\btênis\b` (acento-insensitive). Tênis com `sizes` preenchido precisa ter `"42"` ou `"43"`; tênis sem `sizes` (caso BaW) passa direto (safety: melhor mostrar do que perder).

Logs por source: `>>> netshoes: 163 produtos (207 brutos; -0 gênero/idade, -44 tamanho 42/43)`.

Quando a vocabulary list mudar (ex.: adicionar token novo ao `_BLOCK_TOKENS`), o filtro só vale pra ingestões futuras — produtos antigos continuam no DB até rodar `purge`.

### Subcomando `purge`

Aplica os mesmos filtros sobre o DB existente, usando a última observação como referência. Default é **dry-run** (lista o que removeria); requer `--apply` pra deletar de fato. Operação em transação única; remove de `products` e `price_history` em cascata. Faça backup do `data/prices.db` antes (`cp data/prices.db data/prices.db.bak.$(date -u +%Y%m%dT%H%M%SZ)`).

```bash
PYTHONPATH=src python -m ous_monitor.cli purge          # dry-run
PYTHONPATH=src python -m ous_monitor.cli purge --apply  # executa
PYTHONPATH=src python -m ous_monitor.cli status         # saúde das fontes
```

## Notifier

`notifier.py` posts the new-promotion list to a Telegram chat (one message per ~3.8k chars, HTML mode, links unprevied). Configured via `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in `.env`. The CLI loads `.env` automatically (precedence: real env > .env file). `--dry-run-telegram` formats messages and logs them without sending; `--no-telegram` skips entirely. Failures in the notifier never abort the run — they're logged and swallowed.

**Modo resumo (alta carga).** Para não inundar o chat quando há muitas mudanças, `send_alert`/`send_digest` podem emitir um resumo compacto via `build_summary()`: uma linha por item (`{intensidade} -{pct}% {preço} {nome} ⟵ {preço cheio}`), agrupado por tipo de peça segundo `categories.categorize()` (`tenis`/`camisas_time`/`agasalhos`/`acessorios`/`vestuario`/`outros`, buckets mutuamente exclusivos nessa ordem de prioridade), ordenado por maior desconto, com cap `SUMMARY_PER_GROUP` por grupo (`…+K mais`). `weaker`/`price_up` viram rodapé de contagem; `ended` é omitido. Gatilho: `send_alert` resume automaticamente quando o total de mudanças ≥ `SUMMARY_THRESHOLD` (default 15; `summary=None`); `send_digest` resume por default (`summary=True`, passe `False` pro formato antigo de 4 seções). O resumo usa `_chunk_lines` (junta com `\n`, não `\n\n`) e controla o próprio espaçamento. A classificação em `categories.py` é separada de propósito do filtro SQL `services._category_sql` (mexer naquele mudaria as varreduras filtradas do bot).

## Python version

Python 3.8 (system Python). Code uses `from __future__ import annotations` everywhere so `X | Y` and `list[T]` hints don't fail at import time. **Do not** use runtime-evaluated 3.10+ syntax (e.g. `match`, `dataclass(slots=True)`, `isinstance(x, int | str)`).
