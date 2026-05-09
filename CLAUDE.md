# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Daily monitor of the ÖUS footwear brand. Scrapes three retailers, stores price
history in SQLite, and reports newly-discounted products. Designed to run once
per day from cron on a personal Linux machine.

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
from ous_monitor.scrapers.netshoes import NetshoesScraper
ps = NetshoesScraper().fetch_all()
print(len(ps), 'products,', sum(1 for p in ps if p.has_discount), 'on sale')
"
```

## Architecture

Three layers, all under `src/ous_monitor/`:

1. **Scrapers** (`scrapers/{ous,netshoes,centauro}.py`) — each implements the
   `Scraper` protocol from `scrapers/base.py`: a `source` string and a
   `fetch_all() -> list[Product]` method. Each scraper is responsible for its
   own pagination and **must walk all pages** (not just the first viewport's
   worth — see "Pagination contract" below).

2. **Storage** (`storage.py`) — SQLite with two tables: `products` (one row per
   SKU per source) and `price_history` (one row per (source, sku, observed_at)).
   `record_run()` upserts product rows, appends a price observation, and
   returns counters. `find_changes()` uses `LAG` window functions to detect 4
   mutually-exclusive categories of change since a cutoff timestamp:
   `new_promo` (started/deepened a discount), `ended` (back to list price),
   `weaker` (still discounted but discount % shrunk by ≥25% relative — see
   `DISCOUNT_SHRINK_RATIO`), and `price_up` (price rose ≥5% — see
   `PRICE_UP_RATIO`). Priority: new_promo > ended > weaker > price_up. The
   thin wrapper `find_new_promotions()` is kept for backwards compat.

3. **CLI** (`cli.py`) — orchestrates: runs each requested scraper in isolation
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

## Pagination contract

**Every scraper must paginate fully.** This is a hard requirement, not a nice-to-have. Each scraper logs the server-declared total on the first page and continues fetching until the source signals exhaustion. Concretely:

- **OUS** (`scrapers/ous.py`): VTEX returns `206 Partial Content` and a `resources: X-Y/TOTAL` header. Loop `_from`/`_to` by 50 until `start >= total`. ~3 pages.
- **Netshoes** (`scrapers/netshoes.py`): `__INITIAL_STATE__.SearchPage.totalPages` indicates total pages; loop `?page=N` until reached. ~5 pages.
- **Centauro** (`scrapers/centauro.py`): `__NEXT_DATA__` exposes `pagination.last.pageNumber`; loop `?page=N` reusing the same Playwright context. ~6 pages.

If you change a scraper, verify the log line `"<source>: total declarado = ..."` matches the count of products actually returned.

## Site-specific gotchas

- **OUS**: `Discount` field in the JSON is always `null` — derive promotion from `ListPrice > Price`. Color variants are separate `productId`s. Size variants live inside `items[]` and share pricing, so `items[0]` is enough.
- **Netshoes**: prices are integers in **cents** — divide by 100. Brand string is `"ÖUS"` with the umlaut, not `"OUS"`. Pagination is `?page=N`; `?p=N` is silently ignored. Search without `marca=ous` brings non-OUS marketplace items.
- **Centauro**: Akamai BMP. Plain HTTP requests (curl, httpx, even `curl_cffi`) return 403. The VTEX endpoints are also closed. Playwright headless with the system Chrome (`/usr/bin/google-chrome`) works for fresh IPs but the IP can get blocked for hours after a few requests. The scraper raises `CentauroBlocked` on 403 and `CentauroScraper.fetch_all` swallows it (logs a warning, returns `[]`) so one bad source never breaks the run. Do not loop with retries on 403 — it makes the IP block worse.

## Storage location

The default DB path is `<repo>/data/prices.db`. The `data/` directory is gitignored. For ad-hoc inspection: `sqlite3 data/prices.db ".schema"`.

## Notifier

`notifier.py` posts the new-promotion list to a Telegram chat (one message per ~3.8k chars, HTML mode, links unprevied). Configured via `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in `.env`. The CLI loads `.env` automatically (precedence: real env > .env file). `--dry-run-telegram` formats messages and logs them without sending; `--no-telegram` skips entirely. Failures in the notifier never abort the run — they're logged and swallowed.

## Proxy

`scrapers/centauro.py` reads `CENTAURO_PROXY` (falls back to `HTTPS_PROXY`/`HTTP_PROXY`) and passes it to Playwright's `chromium.launch(proxy=...)`. Accepts `http://`, `https://`, `socks5://`, with optional `user:pass@`. Without a proxy the scraper still attempts direct, but Akamai 403s on residential IPs are common and trigger the swallow-and-warn path.

## Python version

Python 3.8 (system Python). Code uses `from __future__ import annotations` everywhere so `X | Y` and `list[T]` hints don't fail at import time. **Do not** use runtime-evaluated 3.10+ syntax (e.g. `match`, `dataclass(slots=True)`, `isinstance(x, int | str)`).
