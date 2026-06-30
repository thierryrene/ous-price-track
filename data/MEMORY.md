# Project Memory — ous-price-monitor

## Setup Status

- **Venv recreated** 2026-06-18 but `ous_monitor` package NOT installed yet.
- Run `pip install -e .` in the venv to complete setup.
- System Python: 3.12.3 (`/usr/bin/python3`). pyproject.toml declares `>=3.8` but actual runtime is 3.12.

## Architecture Decisions

- **Deploy via Docker Compose** on DigitalOcean VPS, proxied by Traefik (Coolify).
- **Domain**: `https://price-monitor.thierryrenematos.tec.br`
- **DB bind-mount**: `./data:/app/data` so container reads/writes the host's `prices.db` directly.
- **Ubuntu 24.04 (Noble)** base image for glibc 2.39 (needed by `google-antigravity` SDK).
- **Telegram bot** has inline GUI menus; plain text messages are ignored (no AI by default).

## Discovered Durable Knowledge

- `prices.db` is ~13.6 MB with historical price data from multiple scrapers.
- `.env` contains `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`.
- Webhook URL: `https://price-monitor.thierryrenematos.tec.br/setup-webhook?url=...`
- Deploy command: `docker compose up -d --build`
- Logs: `docker logs -f ous-price-monitor`

## Gotchas

- The previous venv was broken (missing pip, incomplete). Always verify `.venv/bin/pip` exists after recreation.
- `python3-venv` apt package must be installed before `python3 -m venv` works on this system.

## Patterns

- After venv recreation, run: `apt-get install -y python3.12-venv && python3 -m venv .venv && .venv/bin/pip install -e .`
