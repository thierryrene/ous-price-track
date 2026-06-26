# MIMO.md — Context for MiMo Code Agent

## Project

**ous-price-monitor** — Daily price monitor for streetwear/shoe brands: **OÜS**, **BaW Clothing**, **Adidas**, and **Umbro**.

- **Language:** Python 3.8 (system Python, uses `from __future__ import annotations`)
- **Dependencies:** httpx, selectolax, curl_cffi, FastAPI, uvicorn, google-antigravity, Playwright
- **Database:** SQLite at `data/prices.db` with product snapshots plus `runs`/`source_runs`
- **Notifications:** Telegram bot (alert/digest modes)
- **Deploy:** Docker Compose on VPS Digital Ocean, reverse proxy via Traefik (Coolify)
- **Domain:** `https://price-monitor.thierryrenematos.tec.br`
- **CI/CD:** GitHub Actions runs scrapers 2×/day (12h/21h UTC)

## Architecture

Three layers under `src/ous_monitor/`:

1. **Sources registry** (`sources.py`) — one source of truth for scraper factory, label, emoji, dashboard colors, CI eligibility and Playwright requirement.
2. **Scrapers** (`scrapers/{ous,netshoes,centauro,baw,umbro}.py`) — each implements `Scraper` protocol: `source` string + `fetch_all() -> list[Product]`. Must fully paginate all pages.
3. **Storage** (`storage.py`) — SQLite with `products`, `price_history`, `runs`, and `source_runs`. `record_run()` upserts + appends observations. `find_changes()` detects 4 categories: `new_promo`, `ended`, `weaker`, `price_up`.
3. **CLI** (`cli.py`) — orchestrates scraping, persistence, change detection, and notification dispatch.

Additional modules: `filters.py` (gender/size filters), `gender.py` (vocabulary), `sizes.py` (size parsing), `notifier.py` (Telegram), `server.py` (FastAPI webhook), `models.py` (Product dataclass), `html_generator.py` (HTML reports).

## Active Sources

| Key | Store | Brand | Items | Notes |
|---|---|---|---|---|
| `ous` | ous.com.br/garimpo | ÖUS | ~144 | VTEX API, ListPrice>Price = promo |
| `netshoes` | clube.netshoes.com.br | ÖUS | ~204 | HTML parse `__INITIAL_STATE__`, prices in cents |
| `centauro` | centauro.com.br | ÖUS | varies | Playwright, Akamai BMP often blocks |
| `baw` | bawclothing.com.br | BaW Clothing | ~587 | Wake/FBits, JSON-LD + dataLayer combo |
| `netshoes_baw` | clube.netshoes.com.br | BaW Clothing | ~50 | Same as netshoes, filtered by marca |
| `netshoes_adidas` | clube.netshoes.com.br | Adidas | ~6900 | 164 pages, ~4min scraping, no Adidas Originals |
| `netshoes_adidas_originals` | clube.netshoes.com.br | Adidas Originals | ~92 | 4 pages, marca separada (marca=adidas-originals) |
| `umbro` | umbro.com.br/outlet | Umbro | ~889 | VTEX coleção 921 |

## Commands

All commands require venv active: `source .venv/bin/activate`

```bash
# Run all scrapers + persist + show new promos
PYTHONPATH=src python -m ous_monitor.cli run

# Single source
PYTHONPATH=src python -m ous_monitor.cli run --sources ous

# Historical report (no network)
PYTHONPATH=src python -m ous_monitor.cli report --days 7

# Current sale snapshot
PYTHONPATH=src python -m ous_monitor.cli list --limit 50

# Operational source status
PYTHONPATH=src python -m ous_monitor.cli status

# Dry-run filter cleanup
PYTHONPATH=src python -m ous_monitor.cli purge

# Docker deploy (on server)
docker compose up -d --build

# View logs
docker logs -f ous-price-monitor

# Force webhook setup
curl -s https://price-monitor.thierryrenematos.tec.br/setup-webhook?url=https://price-monitor.thierryrenematos.tec.br
```

## CLI Modes

- `--mode alert` (default): sends everything changed this run
- `--mode digest`: groups 24h changes into 4 sections
- `--dry-run-telegram`: formats messages without sending
- `--no-telegram`: skips notification entirely
- `-v`: verbose/debug logging

## Pagination Contract (CRITICAL)

Every scraper MUST walk all pages. Each logs server-declared total on first page.

- **OUS**: VTEX `resources: X-Y/TOTAL` header, loop `_from`/`_to` by 50. ~3 pages.
- **Netshoes**: `__INITIAL_STATE__.SearchPage.totalPages`, loop `?page=N`. ~5 (ÖUS), ~2 (BaW), ~164 (Adidas).
- **Centauro**: `__NEXT_DATA__` pagination, loop `?page=N`. ~6 pages.
- **BaW**: JSON-LD `ItemList.numberOfItems`, loop `?pagina=N&tamanho=24` (BOTH required, `?pagina` alone is silently ignored). ~25 pages.

## Site-Specific Gotchas

- **OUS**: `Discount` field always null — derive promo from `ListPrice > Price`. Color variants = separate `productId`s. Size variants share pricing via `items[]`.
- **Netshoes**: Prices in **cents** (divide by 100). Brand `ÖUS` with umlaut (not `OUS`). Pagination `?page=N` (`?p=N` ignored).
- **Centauro**: Akamai BMP. Plain HTTP gets 403. Playwright headless works for fresh IPs but blocks after a few requests. Raises `CentauroBlocked`, scraper swallows and returns `[]`.
- **BaW**: Wake/FBits platform (NOT VTEX). `discount` in dataLayer is **absolute BRL value saved**, NOT percentage — `list_price = price + discount`. Do NOT request `br` in Accept-Encoding. Pagination needs `?pagina=N&tamanho=24` together.

## Filters

Two-stage filtering in `filters.py` before DB ingestion:

1. **Gender/age** (`gender.is_male_or_unisex`): rejects feminine-exclusive, children's, maternity items
2. **Size 42/43** (`passes_size_filter`): only for items with `tênis` in name; must have size 42 or 43 in `sizes` field

Purge subcommand applies same filters to existing DB (dry-run by default, `--apply` to execute). Always backup DB first.

## Environment Variables

From `.env` (auto-loaded by CLI):
- `TELEGRAM_BOT_TOKEN` — Telegram bot token
- `TELEGRAM_CHAT_ID` — Chat ID for notifications
- `CENTAURO_PROXY` (optional) — Proxy for Centauro scraper (`socks5://`, `http://`, `https://`)
- `GEMINI_API_KEY` — For AI chat features in Telegram bot
- `WEBHOOK_ADMIN_TOKEN` — protects `/setup-webhook` and `/status`
- `TELEGRAM_WEBHOOK_SECRET` — Telegram secret token validated in `/webhook`
- `TELEGRAM_ALLOWED_CHAT_IDS` — optional comma-separated chat allowlist

## Deployment Details

- **Container base:** Ubuntu 24.04 (Noble) for glibc 2.39 compatibility
- **DB persistence:** bind-mount `./data:/app/data` (not Docker volume)
- **Port:** 8000 exposed internally, no host port binding (Traefik handles routing)
- **Webhook:** configured at `https://price-monitor.thierryrenematos.tec.br/setup-webhook`

## Bot Interface (Telegram)

FastAPI server (`server.py`) with inline keyboard menus:
- **Main Menu**: Consult DB, Run Scrapers, General Snapshot, Scan Status
- **DB Menu**: ÖUS, Netshoes (groups all 3), Centauro — SQLite queries returning active promos
- **Scrapers Menu**: Individual or bulk (`Rodar Todas`) scraper triggers

Text messages (non-command) return menu prompt — AI chat is currently disabled.

## No Test Suite

There are no automated tests. Validate scraper changes with:
```bash
PYTHONPATH=src python -c "
from ous_monitor.scrapers.baw import BawScraper
ps = BawScraper().fetch_all()
print(len(ps), 'products,', sum(1 for p in ps if p.has_discount), 'on sale')
"
```

## Important Rules

- Python 3.8 — no runtime `match`, no `dataclass(slots=True)`, no `isinstance(x, int | str)`
- Always `from __future__ import annotations`
- Never add comments unless asked
- Prefer `bat`/`rg`/`fd`/`sd`/`eza` over `cat`/`grep`/`find`/`sed`/`ls`
- No "Co-Authored-By" or AI attribution in commits
- Conventional commits only
