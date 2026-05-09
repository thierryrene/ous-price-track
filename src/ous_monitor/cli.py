"""CLI principal do ous-price-monitor.

Subcomandos:
  run        — roda os scrapers, persiste resultados, imprime promoções novas
  report     — mostra promoções novas desde uma data sem rodar scraper
  list       — lista todos os produtos atualmente em promoção (snapshot mais recente)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dotenv import load_dotenv
from .models import Product
from .notifier import TelegramConfigError, send_alert, send_digest
from .scrapers.centauro import CentauroScraper
from .scrapers.netshoes import NetshoesScraper
from .scrapers.ous import OusScraper
from .storage import connect, find_changes, find_new_promotions, record_run

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "prices.db"
DEFAULT_ENV = REPO_ROOT / ".env"

SCRAPERS = {
    "ous": OusScraper,
    "netshoes": NetshoesScraper,
    "centauro": CentauroScraper,
}

log = logging.getLogger("ous_monitor")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _fmt_brl(v: float | None) -> str:
    if v is None:
        return "    —    "
    return f"R$ {v:>8,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _print_promo_row(p: Product) -> None:
    print(
        f"  [{p.source:8}] {p.name[:55]:55} "
        f"{_fmt_brl(p.price)} (de {_fmt_brl(p.list_price)}) "
        f"-{p.discount_pct:.0f}%  {p.url}"
    )


def cmd_run(args: argparse.Namespace) -> int:
    sources = args.sources or list(SCRAPERS)
    now = datetime.now(timezone.utc)
    # Janela de detecção:
    # - alert: últimos 10s (= o que foi inserido nesta execução)
    # - digest: últimas 24h (consolida o dia inteiro de mudanças)
    if args.mode == "digest":
        cutoff_dt = now - timedelta(hours=args.digest_hours)
    else:
        cutoff_dt = now - timedelta(seconds=10)
    cutoff_iso = cutoff_dt.isoformat(timespec="seconds")

    all_products: list[Product] = []
    failed: list[str] = []
    for name in sources:
        scraper_cls = SCRAPERS.get(name)
        if not scraper_cls:
            log.error("Fonte desconhecida: %s", name)
            failed.append(name)
            continue
        try:
            log.info(">>> %s: iniciando scraping", name)
            products = scraper_cls().fetch_all()
            log.info(">>> %s: %d produtos", name, len(products))
            all_products.extend(products)
        except Exception:  # noqa: BLE001
            log.exception(">>> %s: falhou", name)
            failed.append(name)

    if not all_products:
        log.warning("Nenhum produto coletado. Encerrando.")
        return 1 if failed else 0

    with connect(args.db) as conn:
        counters = record_run(conn, all_products)
        changes = find_changes(conn, cutoff_iso)

    log.info(
        "Resumo: %d novos produtos, %d atualizados, %d quedas, %d novas promo, "
        "%d acabaram, %d enfraqueceram, %d subiram",
        counters["new"], counters["updated"], counters["price_drop"],
        len(changes["new_promo"]), len(changes["ended"]),
        len(changes["weaker"]), len(changes["price_up"]),
    )

    total_changes = sum(len(v) for v in changes.values())
    if total_changes:
        print(f"\n=== {total_changes} mudança(s) detectada(s) ({args.mode}) ===")
        for cat, label in [
            ("new_promo", "🆕 promo nova"),
            ("ended", "🔚 acabou"),
            ("weaker", "📉 enfraqueceu"),
            ("price_up", "📈 subiu"),
        ]:
            for row in changes[cat]:
                pct = (int(round((1 - row["price"] / row["list_price"]) * 100))
                       if row["list_price"] else 0)
                print(f"  [{row['source']:8}] {label:18} "
                      f"{row['name'][:45]:45} "
                      f"{_fmt_brl(row['price'])} (de {_fmt_brl(row['list_price'])}) "
                      f"-{pct}%  {row['url']}")
    else:
        print("\nNenhuma mudança nesta execução.")

    if not args.no_telegram and total_changes:
        try:
            if args.mode == "digest":
                period = (now - timedelta(hours=args.digest_hours)).strftime("%d/%m %Hh")
                period_label = f"últimas {args.digest_hours}h (desde {period})"
                send_digest(changes, period_label=period_label,
                            dry_run=args.dry_run_telegram)
            else:
                send_alert(changes, dry_run=args.dry_run_telegram)
        except TelegramConfigError as e:
            log.warning("Telegram não configurado: %s", e)
        except Exception:
            log.exception("Falha ao enviar Telegram (continuando).")

    return 1 if failed and not all_products else 0


def cmd_report(args: argparse.Namespace) -> int:
    since_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
    since_iso = since_dt.isoformat(timespec="seconds")
    with connect(args.db) as conn:
        rows = find_new_promotions(conn, since_iso)
    if not rows:
        print(f"Nenhuma promoção nova nos últimos {args.days} dia(s).")
        return 0
    print(f"=== {len(rows)} promoção(ões) nova(s) nos últimos {args.days} dia(s) ===")
    for r in rows:
        list_price = r["list_price"]
        price = r["price"]
        pct = round((1 - price / list_price) * 100) if list_price else 0
        print(
            f"  [{r['source']:8}] {r['name'][:55]:55} "
            f"{_fmt_brl(price)} (de {_fmt_brl(list_price)}) "
            f"-{pct}%  {r['url']}"
        )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        rows = list(conn.execute(
            """
            SELECT p.source, p.sku, p.name, p.url, h.list_price, h.price
              FROM products p
              JOIN price_history h
                ON h.source = p.source AND h.sku = p.sku
                AND h.observed_at = (
                    SELECT MAX(observed_at) FROM price_history
                     WHERE source = p.source AND sku = p.sku
                )
             WHERE h.list_price IS NOT NULL AND h.list_price > h.price
             ORDER BY (1.0 - h.price * 1.0 / h.list_price) DESC
            """
        ))
    if not rows:
        print("Nenhum produto em promoção no último snapshot.")
        return 0
    print(f"=== {len(rows)} produtos em promoção (último snapshot) ===")
    for r in rows[: args.limit]:
        pct = round((1 - r["price"] / r["list_price"]) * 100)
        print(
            f"  [{r['source']:8}] {r['name'][:55]:55} "
            f"{_fmt_brl(r['price'])} (de {_fmt_brl(r['list_price'])}) "
            f"-{pct}%  {r['url']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ous-monitor")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="caminho do SQLite (default: data/prices.db)")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV, help="caminho do .env (default: ./.env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="roda scrapers e detecta mudanças")
    p_run.add_argument(
        "--sources", nargs="+", choices=list(SCRAPERS),
        help="fontes a rodar (default: todas)",
    )
    p_run.add_argument(
        "--mode", choices=["alert", "digest"], default="alert",
        help="alert: notifica só o que mudou nesta execução (default). "
             "digest: agrupa últimas 24h em 4 seções (use 1×/dia).",
    )
    p_run.add_argument(
        "--digest-hours", type=int, default=24,
        help="janela de horas para o modo digest (default: 24)",
    )
    p_run.add_argument("--no-telegram", action="store_true",
                       help="não enviar notificação Telegram nesta execução")
    p_run.add_argument("--dry-run-telegram", action="store_true",
                       help="formata as mensagens Telegram e loga em vez de enviar")
    p_run.set_defaults(func=cmd_run)

    p_rep = sub.add_parser("report", help="lista promoções novas detectadas no histórico")
    p_rep.add_argument("--days", type=int, default=1, help="janela em dias (default: 1)")
    p_rep.set_defaults(func=cmd_report)

    p_list = sub.add_parser("list", help="lista produtos em promoção no snapshot mais recente")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    n = load_dotenv(args.env)
    if n:
        log.debug("dotenv: %d variáveis carregadas de %s", n, args.env)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
