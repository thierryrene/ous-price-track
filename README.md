# ous-price-monitor

Monitor diário de promoções das marcas **ÖUS**, **BaW Clothing**, **Adidas**,
**Umbro** e **Approve** (Adidas só no Clube Netshoes). Varre 8 fontes (7 no cron
diário + Approve on-demand), guarda histórico de preços em
SQLite e relata produtos que **entraram em promoção** desde a última execução.

## Fontes

| Fonte | URL | Estratégia | Status |
|---|---|---|---|
| `ous` | `ous.com.br/garimpo` (outlet oficial) | API VTEX pública (`catalog_system/pub/products/search`) | ✅ ~144 produtos |
| `umbro` | `umbro.com.br/outlet` (outlet oficial) | API VTEX pública (`catalog_system/pub/products/search`, coleção `921`) | ✅ ~889 produtos |
| `netshoes` | `clube.netshoes.com.br/busca?q=ous&marca=ous` (preço de assinante) | HTML + parse de `window.__INITIAL_STATE__` | ✅ ~204 produtos |
| `baw` | `bawclothing.com.br/roupas/?pagina=N&tamanho=24` (catálogo completo) | HTML SSR Wake/FBits — combina JSON-LD `ItemList` com dataLayer `Hotsite products` (match por item_id no slug) | ✅ ~587 produtos |
| `netshoes_baw` | `clube.netshoes.com.br/busca?marca=baw-clothing` | Mesma estratégia da fonte `netshoes`, filtrando por marca BaW Clothing | ✅ ~50 produtos |
| `netshoes_adidas` | `clube.netshoes.com.br/busca?marca=adidas` | Mesma estratégia da fonte `netshoes`, filtrando por marca Adidas. **Catálogo grande**: ~164 páginas, ~4min de scraping; não inclui Adidas Originals (linha separada) | ✅ ~6900 produtos |
| `netshoes_adidas_originals` | `clube.netshoes.com.br/busca?marca=adidas-originals` | Mesma estratégia da fonte `netshoes`, linha Adidas Originals separada | ✅ ~92 produtos |
| `approve` | `justapprove.com.br/sale` | HTML Tiendanube + parse de listagem | ✅ |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # edite e preencha TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

Todas as fontes usam apenas `httpx`/`selectolax` (sem navegador).

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

No GitHub Actions a manhã (12h UTC) roda `alert` e a tarde (21h UTC = 18h BRT)
roda `digest`, te dando um resumo consolidado no fim do dia.

#### Resumo de alta carga

Quando chega **muita** promoção de uma vez, o bloco-por-produto vira uma parede
de texto. Para isso o notifier tem o **modo resumo**: uma linha por item,
agrupado por **tipo de peça** (👟 tênis/calçados · ⚽ camisas de time ·
🧥 agasalhos · 🧢 acessórios · 👕 vestuário · 📦 outros), ordenado por maior
desconto dentro de cada grupo. Cada linha mostra desconto, preço, nome e preço
cheio: `🔥 -62% R$ 189 Tênis Adidas Forum ⟵ R$ 499`. `📉 piorou`/`📈 subiu`
viram um rodapé de contagem; `🔚 acabou` é omitido.

Quando entra:

- **`alert`**: automaticamente quando o nº de mudanças passa de `SUMMARY_THRESHOLD`
  (default 15). Abaixo disso, mantém os blocos detalhados.
- **`digest`** (e `snapshot`): sempre resumo.

Ajustes por env (ver `.env.example`): `SUMMARY_THRESHOLD` (limiar do alert) e
`SUMMARY_PER_GROUP` (máx. de itens por grupo antes do `…+K mais`, default 12).
A classificação de tipo de peça vive em [src/ous_monitor/categories.py](src/ous_monitor/categories.py).

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

Para produção via webhook, defina também:

- `WEBHOOK_ADMIN_TOKEN` — protege `/setup-webhook` e `/status`.
- `TELEGRAM_WEBHOOK_SECRET` — secret enviado ao Telegram e validado em `/webhook`.
- `TELEGRAM_ALLOWED_CHAT_IDS` — lista opcional de chats permitidos; se vazio,
  o servidor usa `TELEGRAM_CHAT_ID`.

Configuração segura do webhook:

```bash
curl "https://price-monitor.thierryrenematos.tec.br/setup-webhook?url=https://price-monitor.thierryrenematos.tec.br&token=$WEBHOOK_ADMIN_TOKEN"
```

Para validar sem disparar mensagem real:

```bash
ous-monitor run --dry-run-telegram
```

Para pular envio nesta execução: `--no-telegram`.

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

# Saúde operacional das fontes
ous-monitor status

# Verboso (DEBUG)
ous-monitor -v run
```

Banco SQLite em `data/prices.db` (override com `--db /caminho/outro.db`).

O banco registra execuções em `runs` e `source_runs`, além dos snapshots de
preço em `price_history`. Isso permite auditar a última execução bem-sucedida
por fonte, contagens brutas/filtradas e falhas parciais.

## Execução automatizada

### GitHub Actions + Coolify (recomendado)

O workflow [.github/workflows/monitor.yml](.github/workflows/monitor.yml) roda
o scraper 2× ao dia (12h e 21h UTC = 9h e 18h BRT) e commita o
`data/prices.db` atualizado de volta no repo — assim o histórico persiste
entre execuções.

O servidor FastAPI no Coolify fica para o bot do Telegram e ações on-demand
via webhook. Para expor o webhook com segurança, configure também:

- `TELEGRAM_WEBHOOK_SECRET` — enviado ao Telegram no `setWebhook` e validado no header
- `TELEGRAM_ALLOWED_CHAT_IDS` — lista de chats autorizados, separada por vírgula
- `WEBHOOK_ADMIN_TOKEN` — protege as rotas `/setup-webhook` e `/status`
  (o nome legado `ADMIN_TOKEN` também é aceito)

Se o Coolify/VPS também rodar scrapers on-demand pelo Telegram, escolha uma
fonte oficial de verdade para o banco: GitHub Actions versionando `prices.db`
ou o volume persistente da VPS. Rodar os dois sem sincronização pode deixar o
dashboard e o bot olhando históricos diferentes.

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

**Approve não é executada no Actions por padrão.** O workflow roda as fontes
`ous umbro netshoes baw netshoes_baw netshoes_adidas netshoes_adidas_originals`.
Para incluir a Approve, rode local/VPS ou use o bot on-demand no servidor.

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

Cada execução tem um `run_id`; as fontes individuais são registradas em
`source_runs`. Esse histórico operacional é usado pelo comando `status` e pelo
payload do dashboard.

## Pegadinhas conhecidas

- **OUS:** variações de cor são `productId`s diferentes — o mesmo modelo aparece
  várias vezes. Variações de tamanho ficam dentro de `items[]` e dividem o mesmo
  preço, então usamos só `items[0]`.
- **Netshoes:** preços vêm em **centavos** (int) no JSON. A marca canônica é
  `ÖUS` (com umlaut). A paginação é `?page=N`, **não** `?p=N`. Rate-limita (429)
  IPs compartilhados — o scraper repete com backoff exponencial antes de desistir.
