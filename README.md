# ous-price-monitor

Monitor diário de promoções da marca **ÖUS** (calçados). Varre 3 fontes, guarda
histórico de preços em SQLite e relata produtos que **entraram em promoção**
desde a última execução.

## Fontes

| Fonte | URL | Estratégia | Status |
|---|---|---|---|
| `ous` | `ous.com.br/garimpo` (outlet oficial) | API VTEX pública (`catalog_system/pub/products/search`) | ✅ ~144 produtos |
| `netshoes` | `clube.netshoes.com.br/busca?q=ous&marca=ous` (preço de assinante) | HTML + parse de `window.__INITIAL_STATE__` | ✅ ~204 produtos |
| `centauro` | `centauro.com.br/busca/ous` | Playwright headless + parse de `__NEXT_DATA__` | ⚠️ Akamai BMP — frequentemente bloqueia. Não derruba o pipeline (loga warning e segue). |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # edite e preencha TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

Para a fonte Centauro funcionar é preciso ter `/usr/bin/google-chrome` instalado
(o scraper aproveita o Chrome do sistema em vez de baixar o do Playwright).

### Notificação Telegram

Cada `run` envia ao seu chat as promoções **novas** detectadas (uma mensagem
HTML com nome, preço de/por, % desconto e link clicável). Se houver muitas
promoções a mensagem é particionada em chunks de até ~3.8k chars.

Como obter as credenciais:

1. Fale com `@BotFather` no Telegram → `/newbot` → copie o TOKEN.
2. Mande qualquer mensagem ao bot e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` no navegador.
   O número em `"chat":{"id": ...}` é seu CHAT_ID.
3. Cole ambos no `.env`.

Para validar sem disparar mensagem real:

```bash
ous-monitor run --dry-run-telegram
```

Para pular envio nesta execução: `--no-telegram`.

### Proxy para Centauro

A Centauro está atrás de Akamai BMP e costuma bloquear IPs residenciais por
horas após algumas requisições. Se quiser confiabilidade, configure
`CENTAURO_PROXY` no `.env` apontando pra uma VPN/proxy SOCKS local
(`socks5://127.0.0.1:1080` é o típico do `ssh -D` ou de clientes Mullvad/Tor).

## Uso

```bash
# Rodar scrapers, persistir e mostrar promoções novas detectadas neste run
ous-monitor run

# Apenas algumas fontes
ous-monitor run --sources ous netshoes

# Relatório retroativo a partir do histórico (sem rodar scraper)
ous-monitor report --days 7

# Snapshot atual: tudo que está em promoção agora
ous-monitor list --limit 100

# Verboso (DEBUG)
ous-monitor -v run
```

Banco SQLite em `data/prices.db` (override com `--db /caminho/outro.db`).

## Cron (rodar 1x/dia às 9h)

```cron
0 9 * * * cd /home/thierry/Desktop/web_testes/ous-price-monitor && \
  .venv/bin/python -m ous_monitor.cli run >> data/run.log 2>&1
```

## Como funciona a detecção de promoção nova

Cada execução grava um snapshot em `price_history`. Um produto é considerado
"promoção nova" quando:

- O snapshot atual tem `list_price > price` (há desconto), **e**
- O snapshot imediatamente anterior do mesmo SKU **não tinha** desconto (ou
  tinha um preço maior).

Isso é calculado via SQL window function (`LAG`) em `storage.find_new_promotions`.

## Pegadinhas conhecidas

- **OUS:** variações de cor são `productId`s diferentes — o mesmo modelo aparece
  várias vezes. Variações de tamanho ficam dentro de `items[]` e dividem o mesmo
  preço, então usamos só `items[0]`.
- **Netshoes:** preços vêm em **centavos** (int) no JSON. A marca canônica é
  `ÖUS` (com umlaut). A paginação é `?page=N`, **não** `?p=N`.
- **Centauro:** o Akamai pode queimar o IP por horas. O pipeline trata o 403
  como aviso, retorna lista vazia da fonte e segue com as outras.
