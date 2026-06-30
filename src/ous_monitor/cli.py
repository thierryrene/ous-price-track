"""CLI principal do ous-price-monitor.

Subcomandos:
  run        — roda os scrapers, persiste resultados, notifica MUDANÇAS
               (promo nova / acabou / piorou / subiu) desde a última janela
  snapshot   — roda os scrapers, persiste, notifica TUDO em promoção agora
               (ignora 'já notificou')
  report     — mostra promoções novas desde uma data sem rodar scraper
  list       — lista todos os produtos atualmente em promoção (snapshot mais recente)
  purge      — remove do DB produtos que falham os filtros (default dry-run)

Filtros de ingestão (gênero/idade + tênis 42/43) vivem em `filters.py` e são
aplicados em `_scrape_and_persist`. O DB é considerado fonte da verdade — o
notifier não filtra mais nada.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dotenv import load_dotenv
from .models import Product
from .notifier import TelegramConfigError, send_alert, send_digest
from .services import CatalogService, MonitorService, SourceRegistry
from .storage import connect, find_new_promotions

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "data" / "prices.db")))
DEFAULT_ENV = Path(os.environ.get("ENV_PATH", str(REPO_ROOT / ".env")))

SCRAPERS = SourceRegistry.all()

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


def _scrape_and_persist(args: argparse.Namespace) -> tuple[list[Product], list[str], dict]:
    """Roda os scrapers solicitados, persiste no DB, devolve (produtos, falhas, counters).
    Compartilhado por cmd_run e cmd_snapshot."""
    result = MonitorService(args.db).scrape_and_persist(args.sources)
    return result.products, result.failed, result.counters.as_dict()


def cmd_run(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    result = MonitorService(args.db).run(
        sources=args.sources,
        mode=args.mode,
        digest_hours=args.digest_hours,
    )
    all_products = result.scrape.products
    failed = result.scrape.failed
    counters = result.scrape.counters.as_dict()
    changes = result.changes
    if not all_products:
        log.warning("Nenhum produto coletado. Encerrando.")
        return 1 if failed else 0

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
                       if row["list_price"] and row["list_price"] > 0 else 0)
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
                send_alert(changes, dry_run=args.dry_run_telegram,
                           period_label=now.strftime("%d/%m %Hh UTC"))
        except TelegramConfigError as e:
            log.warning("Telegram não configurado: %s", e)
        except Exception:
            log.exception("Falha ao enviar Telegram (continuando).")

    try:
        from .html_generator import write_dashboard
        log.info("Atualizando dashboard HTML em data/produtos.html...")
        with connect(args.db) as conn:
            write_dashboard(conn, REPO_ROOT / "data" / "produtos.html")
    except Exception:
        log.exception("Falha ao regerar dashboard HTML automaticamente (continuando).")

    return 1 if failed and not all_products else 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Roda scrapers, persiste no DB, e manda um digest com TODOS os produtos
    em promoção agora — independente de já terem sido notificados.
    O filtro de tamanhos (42/43 pra tênis) continua valendo.
    """
    now = datetime.now(timezone.utc)
    result = MonitorService(args.db).snapshot(sources=args.sources)
    all_products = result.scrape.products
    failed = result.scrape.failed
    counters = result.scrape.counters.as_dict()
    changes = result.changes
    if not all_products:
        log.warning("Nenhum produto coletado. Encerrando.")
        return 1 if failed else 0

    total = len(changes["new_promo"])
    log.info(
        "Snapshot: %d novos produtos, %d atualizados; %d em promoção agora.",
        counters.get("new", 0), counters.get("updated", 0), total,
    )

    if total == 0:
        print("\nNenhum produto em promoção no momento.")
        return 0

    print(f"\n=== {total} produto(s) em promoção (snapshot completo) ===")
    for row in changes["new_promo"]:
        pct = (int(round((1 - row["price"] / row["list_price"]) * 100))
               if row["list_price"] and row["list_price"] > 0 else 0)
        print(f"  [{row['source']:8}] "
              f"{row['name'][:55]:55} "
              f"{_fmt_brl(row['price'])} (de {_fmt_brl(row['list_price'])}) "
              f"-{pct}%  {row['url']}")

    if not args.no_telegram:
        try:
            label = now.strftime("snapshot %d/%m %Hh UTC")
            send_digest(changes, period_label=label,
                        dry_run=args.dry_run_telegram)
        except TelegramConfigError as e:
            log.warning("Telegram não configurado: %s", e)
        except Exception:
            log.exception("Falha ao enviar Telegram (continuando).")

    try:
        from .html_generator import write_dashboard
        log.info("Atualizando dashboard HTML em data/produtos.html...")
        with connect(args.db) as conn:
            write_dashboard(conn, REPO_ROOT / "data" / "produtos.html")
    except Exception:
        log.exception("Falha ao regerar dashboard HTML automaticamente (continuando).")

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
        pct = round((1 - price / list_price) * 100) if list_price and list_price > 0 else 0
        print(
            f"  [{r['source']:8}] {r['name'][:55]:55} "
            f"{_fmt_brl(price)} (de {_fmt_brl(list_price)}) "
            f"-{pct}%  {r['url']}"
        )
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """Remove do DB produtos que não passam pelos filtros atuais (gênero/idade
    e tênis 42/43), usando a última observação registrada como referência.

    Default é dry-run; passar --apply faz a deleção em transação única.
    """
    from collections import Counter

    service = CatalogService(args.db)
    result = service.purge_apply() if args.apply else service.purge_candidates()
    to_drop = result.candidates

    if not to_drop:
        print("DB já está limpo — nenhum produto a remover.")
        return 0

    by_src = Counter(d.source for d in to_drop)
    by_reason = Counter(d.reason for d in to_drop)

    mode = "APLICANDO" if args.apply else "Dry-run (use --apply pra executar)"
    print(f"=== Purge — {mode} ===")
    print("Critérios: gênero/idade + tênis 42/43 (filtros.py)\n")
    print("Por source:")
    for s in sorted(by_src):
        g = sum(1 for d in to_drop if d.source == s and d.reason == "gender")
        z = sum(1 for d in to_drop if d.source == s and d.reason == "size")
        print(f"  {s:18} {by_src[s]:4} a remover  ({g} gênero, {z} tamanho)")
    print(f"\nTotal: {len(to_drop)} produtos "
          f"({by_reason.get('gender', 0)} por gênero/idade, "
          f"{by_reason.get('size', 0)} por tamanho)")

    print(f"\nAmostra ({min(10, len(to_drop))} primeiros):")
    for c in to_drop[:10]:
        print(f"  [{c.source:14}] ({c.reason:6}) {c.name[:70]}")

    print(f"\nObservações em price_history a remover em cascata: {result.observations}.")

    if not args.apply:
        print("\nDry-run: nada foi modificado. Rode novamente com --apply pra deletar.")
        return 0

    print(f"\n✔ Removidos {len(to_drop)} produtos e {result.observations} observações.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = CatalogService(args.db).latest_discounted(limit=args.limit)
    if not rows:
        print("Nenhum produto em promoção no último snapshot.")
        return 0
    print(f"=== {len(rows)} produtos em promoção (último snapshot) ===")
    for r in rows:
        pct = round((1 - r["price"] / r["list_price"]) * 100) if r["list_price"] and r["list_price"] > 0 else 0
        print(
            f"  [{r['source']:8}] {r['name'][:55]:55} "
            f"{_fmt_brl(r['price'])} (de {_fmt_brl(r['list_price'])}) "
            f"-{pct}%  {r['url']}"
        )
    return 0


def cmd_export_html(args: argparse.Namespace) -> int:
    from .html_generator import write_dashboard
    log.info("Gerando dashboard HTML interativo em %s...", args.output)
    with connect(args.db) as conn:
        write_dashboard(conn, args.output)
    print(f"✔ Dashboard gerado com sucesso em: {args.output}")
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

    p_snap = sub.add_parser(
        "snapshot",
        help="roda scrapers e envia digest com TODOS os produtos em promoção "
             "agora (ignora 'já notificou'; filtros gênero/tamanho são "
             "aplicados na ingestão)",
    )
    p_snap.add_argument(
        "--sources", nargs="+", choices=list(SCRAPERS),
        help="fontes a rodar (default: todas)",
    )
    p_snap.add_argument("--no-telegram", action="store_true")
    p_snap.add_argument("--dry-run-telegram", action="store_true")
    p_snap.set_defaults(func=cmd_snapshot)

    p_rep = sub.add_parser("report", help="lista promoções novas detectadas no histórico")
    p_rep.add_argument("--days", type=int, default=1, help="janela em dias (default: 1)")
    p_rep.set_defaults(func=cmd_report)

    p_list = sub.add_parser("list", help="lista produtos em promoção no snapshot mais recente")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_list)

    p_purge = sub.add_parser(
        "purge",
        help="remove do DB produtos que falham os filtros de gênero/idade "
             "ou tênis 42/43. Default é dry-run; passe --apply para deletar.",
    )
    p_purge.add_argument("--apply", action="store_true",
                         help="executa a deleção (caso contrário só mostra).")
    p_purge.set_defaults(func=cmd_purge)

    p_html = sub.add_parser(
        "export-html",
        help="gera um dashboard HTML interativo e premium em data/produtos.html",
    )
    p_html.add_argument(
        "--output", type=Path, default=REPO_ROOT / "data" / "produtos.html",
        help="caminho do arquivo HTML gerado (default: data/produtos.html)",
    )
    p_html.set_defaults(func=cmd_export_html)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    n = load_dotenv(args.env)
    if n:
        log.debug("dotenv: %d variáveis carregadas de %s", n, args.env)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
