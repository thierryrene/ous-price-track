# ous-price-monitor

Monitor diário de promoções das marcas **ÖUS**, **BaW Clothing** e **Adidas**
(Adidas só no Clube Netshoes). Varre 6 fontes, guarda histórico de preços em
SQLite e relata produtos que **entraram em promoção** desde a última execução.

## Fontes

| Fonte | URL | Estratégia | Status |
|---|---|---|---|
| `ous` | `ous.com.br/garimpo` (outlet oficial) | API VTEX pública (`catalog_system/pub/products/search`) | ✅ ~144 produtos |
| `netshoes` | `clube.netshoes.com.br/busca?q=ous&marca=ous` (preço de assinante) | HTML + parse de `window.__INITIAL_STATE__` | ✅ ~204 produtos |
| `centauro` | `centauro.com.br/busca/ous` | Playwright headless + parse de `__NEXT_DATA__` | ⚠️ Akamai BMP — frequentemente bloqueia. Não derruba o pipeline (loga warning e segue). |
| `baw` | `bawclothing.com.br/roupas/?pagina=N&tamanho=24` (catálogo completo) | HTML SSR Wake/FBits — combina JSON-LD `ItemList` com dataLayer `Hotsite products` (match por item_id no slug) | ✅ ~587 produtos |
| `netshoes_baw` | `clube.netshoes.com.br/busca?marca=baw-clothing` | Mesma estratégia da fonte `netshoes`, filtrando por marca BaW Clothing | ✅ ~50 produtos |
| `netshoes_adidas` | `clube.netshoes.com.br/busca?marca=adidas` | Mesma estratégia da fonte `netshoes`, filtrando por marca Adidas. **Catálogo grande**: ~164 páginas, ~4min de scraping; não inclui Adidas Originals (linha separada) | ✅ ~6900 produtos |

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

Cada `run` analisa o histórico e detecta **4 categorias de mudança** desde a
última observação:

- 🆕 **Promoção nova** — produto entrou em desconto (ou caiu mais)
- 🔚 **Acabou a promo** — voltou ao preço cheio
- 📉 **Desconto piorou** — ainda em promo, mas o desconto encolheu ≥25%
  (ex: era -40%, virou -15%)
- 📈 **Subiu de preço** — aumento ≥5% sem encerrar promoção (filtro contra ruído)

O CLI tem dois modos de envio (flag `--mode`):

- **`alert`** (default): envia tudo que mudou nesta execução, todas as
  categorias num único bloco com cabeçalho. Pensado para runs de tempo real.
- **`digest`**: agrupa mudanças das últimas 24h em **4 seções separadas**
  (configurável via `--digest-hours`). Pensado para 1×/dia, fim do dia.

No GitHub Actions a manhã (12h UTC) roda `alert` e a tarde (21h UTC) roda
`digest`, te dando um resumo consolidado no fim do dia.

Há ainda o subcomando **`snapshot`** (workflow `snapshot.yml`, só manual):
roda os scrapers e envia um digest com **todos os produtos atualmente em
promoção** — útil pra "varrer o catálogo agora" sem esperar pelo cron.
Mantém o filtro de tamanhos 42/43, mas ignora o filtro "já notifiquei isso
no run anterior".

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

## Execução automatizada

### GitHub Actions (recomendado)

O workflow [.github/workflows/monitor.yml](.github/workflows/monitor.yml) roda
o scraper 2× ao dia (12h e 21h UTC = 9h e 18h BRT) e commita o
`data/prices.db` atualizado de volta no repo — assim o histórico persiste
entre execuções.

Setup único:

1. **Crie um bot Telegram** novo via `@BotFather` (ou reuse o atual).
2. No GitHub, vá em `Settings → Secrets and variables → Actions → New repository secret`
   e crie:
   - `TELEGRAM_BOT_TOKEN` — token do bot
   - `TELEGRAM_CHAT_ID` — id do chat onde notificar
3. Commite o `data/prices.db` inicial (gerado pelo primeiro run local). Sem
   isso, o primeiro run no Actions vai notificar todas as ~263 promos atuais
   de uma vez. Para evitar:

   ```bash
   PYTHONPATH=src python -m ous_monitor.cli run --no-telegram
   git add data/prices.db
   git commit -m "chore(data): snapshot inicial"
   git push
   ```

4. Dispare manualmente no GitHub: `Actions → monitor → Run workflow`.

**Centauro não é executada no Actions** — IPs dos runners GitHub são
agressivamente bloqueados pelo Akamai. O workflow chama
`run --sources ous netshoes`. Para incluir Centauro, rode local com proxy
ou mantenha um cron paralelo na sua máquina.

### Cron local (alternativa)

```cron
0 9 * * * cd /home/thierry/Desktop/web_testes/ous-price-monitor && \
  PYTHONPATH=src .venv/bin/python -m ous_monitor.cli run >> data/run.log 2>&1
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
