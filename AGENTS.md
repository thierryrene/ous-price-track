# AGENTS.md

**Fonte única de orientação para qualquer agente de código** (Claude Code,
Codex, Cursor, Gemini, MiMo, GitHub Copilot, etc.) que trabalhe neste repo.
Os arquivos `CLAUDE.md`, `GEMINI.md`, `MIMO.md` e
`.github/copilot-instructions.md` apenas apontam para cá — **mantenha este
arquivo como a verdade única** e não duplique conteúdo nos ponteiros (foi a
divergência entre docs que já causou um merge gigante neste projeto).

## Projeto

Monitor diário de promoções de streetwear/calçado: **ÖUS**, **BaW Clothing**,
**Adidas**, **Umbro** e **Approve**. Raspa cada marca da loja própria (quando
aplicável) + retailers marketplace, guarda histórico de preços em SQLite,
detecta o que mudou, notifica via Telegram e gera um dashboard HTML. Roda no
GitHub Actions 2×/dia; um servidor FastAPI (Coolify) serve o bot do Telegram
(orientado a botões) e ações on-demand.

### Fontes ativas (8)

Definidas **uma única vez** em [`src/ous_monitor/sources.py`](src/ous_monitor/sources.py)
(`SOURCES`), consumidas por CLI, labels/botões do Telegram, validação do server
e config do dashboard.

| key | loja | marca | estratégia |
|---|---|---|---|
| `ous` | ous.com.br/garimpo | ÖUS | API VTEX pública |
| `umbro` | umbro.com.br/outlet | Umbro | API VTEX (coleção 921), ~889 itens |
| `netshoes` | clube.netshoes.com.br | ÖUS | HTML + `__INITIAL_STATE__`, preços em centavos |
| `baw` | bawclothing.com.br | BaW | HTML SSR Wake/FBits (JSON-LD + dataLayer) |
| `netshoes_baw` | clube.netshoes.com.br | BaW | mesma do `netshoes`, `marca=baw-clothing` |
| `netshoes_adidas` | clube.netshoes.com.br | Adidas | ~6900 itens, ~164 páginas, ~4-8min |
| `netshoes_adidas_originals` | clube.netshoes.com.br | Adidas Originals | `marca=adidas-originals`, ~90 itens |
| `approve` | justapprove.com.br/sale | Approve | HTML Tiendanube. **Só on-demand** (`run_in_ci=False`) |

Cada par (loja, marca) é uma source distinta — não há coluna `brand` no DB; quem
segrega marca dentro do mesmo retailer é o `source_name`. `NetshoesScraper` é
parametrizado por marca; `NetshoesBawScraper` / `NetshoesAdidasScraper` /
`NetshoesAdidasOriginalsScraper` são factories que devolvem instâncias
configuradas. O cron diário roda as 7 fontes com `run_in_ci=True`
(`ci_source_keys()`); `approve` fica de fora e é acionada pelo bot.

> **Não reintroduza Centauro nem o agente de IA "AGY"/google-antigravity** —
> ambos foram removidos de propósito (ver Histórico). A Centauro era bloqueada
> pelo Akamai (Playwright); o AGY era código morto.

## Setup & comandos

```bash
source .venv/bin/activate    # ou: python3 -m venv .venv && pip install -e .

# rodar todos os scrapers do CI, persistir, detectar mudanças e notificar
PYTHONPATH=src python -m ous_monitor.cli run

# uma fonte só (útil iterando num scraper)
PYTHONPATH=src python -m ous_monitor.cli run --sources ous

# relatório histórico (sem rede) / snapshot atual / saúde por fonte
PYTHONPATH=src python -m ous_monitor.cli report --days 7
PYTHONPATH=src python -m ous_monitor.cli list --limit 50
PYTHONPATH=src python -m ous_monitor.cli status

# dashboard HTML / limpeza por filtros (dry-run por padrão)
PYTHONPATH=src python -m ous_monitor.cli export-html
PYTHONPATH=src python -m ous_monitor.cli purge          # --apply para deletar
```

Subcomandos: `run`, `snapshot`, `report`, `list`, `purge`, `export-html`, `status`.
Flags de notificação: `--mode {alert,digest}`, `--no-telegram`, `--dry-run-telegram`.

### Testes

Há suíte em [`tests/`](tests/) (unittest). **Rode antes de commitar:**

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

Para validar um scraper de verdade (rede), chame `fetch_all()` standalone:

```bash
PYTHONPATH=src python -c "
from ous_monitor.scrapers.baw import BawScraper
ps = BawScraper().fetch_all()
print(len(ps), 'produtos,', sum(1 for p in ps if p.has_discount), 'em promo')
"
```

## Arquitetura

Tudo sob `src/ous_monitor/`:

1. **Registro de fontes** (`sources.py`) — `SOURCES: dict[str, SourceConfig]`
   com factory do scraper, label, emoji, cores do dashboard e `run_in_ci`.
   Fonte única; importar é leve (todos os scrapers são httpx/selectolax).
2. **Scrapers** (`scrapers/{ous,netshoes,baw,umbro,approve}.py`) — implementam o
   protocolo `Scraper` de `scrapers/base.py`: string `source` + `fetch_all() ->
   list[Product]`. Cada um **pagina por completo** (ver Contrato de paginação).
3. **Storage** (`storage.py`) — SQLite com `products`, `price_history`, `runs` e
   `source_runs`. `record_run()` faz upsert dos produtos, anexa observação
   ligada a `run_id`, **deduplica SKUs repetidos** e devolve contadores.
   `find_changes()` usa window functions (`LAG`) para detectar 4 categorias
   mutuamente exclusivas (ver abaixo). Pragmas: WAL, `busy_timeout`,
   `foreign_keys`. Run-tracking: `start_run`/`finish_run`/`record_source_run`/
   `latest_source_runs`.
4. **Serviços** (`services.py`) — orquestração (`MonitorService`): pega um **lock
   de arquivo** (`fcntl`, exclusão entre processos cron×bot), roda cada scraper
   isolado (um caindo não derruba os outros), grava run-tracking, persiste e
   detecta mudanças. `CatalogService`: queries de catálogo, `purge`, `normalize`,
   stats. `SourceRegistry` projeta `sources.SOURCES`.
5. **CLI** (`cli.py`) — adaptador fino sobre os serviços + saída no terminal e
   dispatch de notificação.
6. **Notifier** (`notifier.py`) — Telegram (ver abaixo).
7. **Server** (`server.py`) — FastAPI/webhook do bot (ver abaixo).
8. **Dashboard** (`html_generator.py`) — HTML estático regenerado a cada run.
9. Apoio: `filters.py`/`gender.py`/`sizes.py` (filtros de ingestão),
   `categories.py` (tipo de peça para o resumo), `models.py` (`Product` e
   contratos tipados), `dotenv.py`.

O dataclass `Product` em `models.py` é o contrato entre scrapers e storage —
adicione um campo lá se um novo dado precisa fluir.

## Detecção de mudanças (4 categorias)

`find_changes()` compara a última observação com a anterior por SKU e classifica,
priorizando `new_promo > ended > weaker > price_up`:

- 🆕 **new_promo** — entrou em desconto (ou caiu mais).
- 🔚 **ended** — voltou ao preço cheio.
- 📉 **weaker** — ainda em promo, mas o desconto % encolheu ≥25% relativo
  (`DISCOUNT_SHRINK_RATIO`).
- 📈 **price_up** — preço subiu ≥5% (`PRICE_UP_RATIO`).

Modos (`--mode`): `alert` (cutoff `now-10s`, tempo real) e `digest` (janela
`--digest-hours`, default 24h). No GitHub Actions: `alert` às 12h UTC, `digest`
às 21h UTC (= 18h BRT).

## Contrato de paginação

**Todo scraper pagina por completo** — requisito rígido. Cada um loga o total
declarado pelo servidor na 1ª página e segue até esgotar.

- **OUS/Umbro** (VTEX): `206 Partial Content` + header `resources: X-Y/TOTAL`;
  loop `_from`/`_to` de 50 em 50 até `start >= total`.
- **Netshoes**: `__INITIAL_STATE__.SearchPage.totalPages`; loop `?page=N`.
  Produtos em `SearchPage.parentSkus` (não `products`). ~5 págs (ÖUS), ~2 (BaW),
  ~164 (Adidas).
- **BaW**: JSON-LD `ItemList.numberOfItems` declara o total; loop
  `?pagina=N&tamanho=24`. **Pegadinha**: só `?pagina=N` é ignorado e devolve a
  pg 1 — precisa de `tamanho` junto. ~25 págs.

## Gotchas por site

- **OUS**: campo `Discount` no JSON é sempre `null` — derive promoção de
  `ListPrice > Price`. Variações de cor são `productId`s separados; tamanhos
  vivem em `items[]` e dividem preço, então `items[0]` basta.
- **Netshoes**: preços são int em **centavos** (÷100). Marcas: ÖUS é `"ÖUS"`
  (umlaut); BaW vem `"BAW Clothing"` (slug `marca=baw-clothing`; `marca=baw`
  puro é outra marca de 2 itens); Adidas é `"Adidas"` (`marca=adidas`;
  `marca=adidas-originals` à parte, brand `"Adidas Originals"`, NÃO incluído em
  `netshoes_adidas`). Paginação `?page=N` (`?p=N` é ignorado). Sem `marca=...`
  vêm itens de marketplace fora do alvo.
- **Netshoes — rate-limit (429)**: `clube.netshoes.com.br` limita IPs
  compartilhados (runners de CI especialmente). O scraper repete com **backoff
  exponencial** respeitando `Retry-After` — ver `scrapers/netshoes.py:_get_with_retry`.
  Só falha após esgotar as tentativas (fonte isolada, não derruba o run).
- **BaW**: plataforma Wake/FBits (não VTEX — `catalog_system` dá 404). Combina
  JSON-LD `ItemList` (url/imagem/nome/preço) com o JS inline
  `{item_list_name:"Hotsite products"…}` (campo `discount`), casados por
  `item_id` (sufixo do slug). **`discount` é o valor ABSOLUTO em R$ economizado,
  não %** → `list_price = price + discount`. **Não** peça `br` em
  `Accept-Encoding` (httpx só decodifica brotli com pacote extra) — use
  `gzip, deflate`.

## Filtros de ingestão

Vivem em [`filters.py`](src/ous_monitor/filters.py), aplicados em
`services.MonitorService.scrape_and_persist` **antes** de `record_run`. Produtos
rejeitados nunca entram no DB. **O DB é a fonte da verdade** — notifier e
`list`/`report` confiam que o que está lá já passou pelo filtro.

Dois critérios encadeados (gênero antes de tamanho, p/ log determinístico):

1. **Gênero/idade** (`gender.is_male_or_unisex`) — rejeita token feminino-
   exclusivo, infantil/juvenil, maternidade ou categoria feminina exclusiva.
   Sem marcador → aceita (unissex). Vocabulário em `gender.py`.
2. **Tamanho 42/43** (`passes_size_filter`) — só atua em itens com `\btênis\b`
   no nome. Tênis com `sizes` precisa ter `"42"`/`"43"`; tênis sem `sizes` (BaW)
   passa direto.

Quando o vocabulário muda, o filtro só vale para ingestões futuras — rode
`purge` (dry-run por padrão; `--apply` deleta em transação única, cascateando
`products` + `price_history`). Faça backup do `data/prices.db` antes.

## Notifier (Telegram)

`notifier.py` posta no chat (HTML, ~3.8k chars/msg). Configurado por
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` no `.env` (precedência: env real > .env).
`--dry-run-telegram` formata e loga sem enviar; `--no-telegram` pula. Falhas no
notifier nunca abortam o run.

**Modo resumo (alta carga).** Para não inundar o chat, `send_alert`/`send_digest`
podem emitir um resumo via `build_summary()`: **uma linha por item**
(`{intensidade} -{pct}% {preço} {nome} ⟵ {preço cheio}`), agrupado por **tipo de
peça** (`categories.categorize()`: `tenis`/`camisas_time`/`agasalhos`/
`acessorios`/`vestuario`/`outros`, exclusivos nessa ordem), ordenado por maior
desconto, com cap `SUMMARY_PER_GROUP` por grupo (`…+K mais`). `weaker`/`price_up`
viram rodapé de contagem; `ended` é omitido. Gatilho: `send_alert` resume quando
total ≥ `SUMMARY_THRESHOLD` (default 15); `send_digest` resume por padrão
(`summary=False` volta ao formato de 4 seções). `categories.py` é separado do
filtro SQL `services._category_sql` de propósito.

## Server / segurança

`server.py` (FastAPI) expõe `/health`, `/health/ready`, `/status` e `/webhook`.
O bot é **orientado a botões** (menus inline) — texto comum devolve o menu (não
há chat com IA). Variáveis:

- `WEBHOOK_ADMIN_TOKEN` — protege `/setup-webhook` e `/status` (alias legado
  `ADMIN_TOKEN`; param de query `token` ou `admin_token`).
- `TELEGRAM_WEBHOOK_SECRET` — validado no header em `/webhook`.
- `TELEGRAM_ALLOWED_CHAT_IDS` — allowlist (vírgula); vazio → usa `TELEGRAM_CHAT_ID`.
- `SUMMARY_THRESHOLD` / `SUMMARY_PER_GROUP` — ajuste do resumo.

## Deploy

- **Docker:** base `python:3.12-slim` (Debian). Todas as deps são Python puro /
  wheels manylinux — sem navegador. `CMD` sobe `uvicorn ous_monitor.server:app`.
- **GitHub Actions** (`.github/workflows/monitor.yml`): 2×/dia, instala só
  `httpx`+`selectolax`, roda as 7 fontes de CI e commita o `data/prices.db`
  atualizado (estado entre execuções). `snapshot.yml` é manual.
- **Coolify/VPS:** Docker Compose + Traefik; bind-mount `./data:/app/data`.

## Versão do Python

`pyproject.toml` declara `requires-python = ">=3.8"`. O código usa
`from __future__ import annotations` em todo lugar, então hints `X | Y` e
`list[T]` não falham em import. Runtime real: CI usa **3.11**, Docker/dev usam
**3.12**. Para preservar o piso 3.8, **não** use sintaxe 3.10+ avaliada em runtime
(`match`, `dataclass(slots=True)`, `isinstance(x, int | str)`).

## Convenções para agentes

- Rode `python -m unittest discover -s tests` antes de commitar; adicione teste
  ao mexer em storage/services/notifier/scrapers.
- Mudou uma fonte? Atualize só `sources.py` (registro único) — CLI, bot e
  dashboard herdam.
- Mantenha este `AGENTS.md` como verdade única; os arquivos por-agente são
  ponteiros. Ao mudar comportamento, atualize aqui e o `CHANGELOG.md`.
- Não reintroduza Centauro/Playwright nem o AGY/google-antigravity.
- O `data/prices.db` é versionado de propósito (estado entre runs do CI).
