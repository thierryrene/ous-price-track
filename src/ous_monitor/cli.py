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
import fcntl
import logging
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dotenv import load_dotenv
from .filters import should_keep_product
from .models import Product
from .notifier import TelegramConfigError, send_alert, send_digest
from .sources import SOURCES, source_keys
from .storage import (
    connect, find_changes, find_new_promotions, finish_run, record_run,
    latest_source_runs, record_source_run, snapshot_promotions, start_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "prices.db"
DEFAULT_ENV = REPO_ROOT / ".env"

SCRAPERS = {key: cfg.scraper_factory for key, cfg in SOURCES.items()}

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


@contextmanager
def _monitor_lock(db_path: Path, timeout_s: float = 10.0):
    lock_path = db_path.parent / ".monitor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Outro scraping já está em execução; tente novamente em instantes.")
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _scrape_and_persist(args: argparse.Namespace) -> tuple[list[Product], list[str], dict, str]:
    with _monitor_lock(args.db):
        return _scrape_and_persist_locked(args)


def _scrape_and_persist_locked(args: argparse.Namespace) -> tuple[list[Product], list[str], dict, str]:
    """Roda os scrapers solicitados, persiste no DB, devolve (produtos, falhas, counters).
    Compartilhado por cmd_run e cmd_snapshot."""
    sources = args.sources or source_keys()
    all_products: list[Product] = []
    failed: list[str] = []
    run_mode = getattr(args, "mode", "snapshot")
    with connect(args.db) as conn:
        run_id = start_run(conn, mode=run_mode, sources=sources)

    for name in sources:
        scraper_cls = SCRAPERS.get(name)
        source_started = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        if not scraper_cls:
            log.error("Fonte desconhecida: %s", name)
            failed.append(name)
            with connect(args.db) as conn:
                record_source_run(
                    conn, run_id=run_id, source=name, started_at=source_started,
                    status="failed", error="Fonte desconhecida",
                )
            continue
        try:
            log.info(">>> %s: iniciando scraping", name)
            products = scraper_cls().fetch_all()
            kept: list[Product] = []
            drop_g = drop_s = 0
            for p in products:
                ok, reason = should_keep_product(p)
                if ok:
                    kept.append(p)
                elif reason == "gender":
                    drop_g += 1
                else:
                    drop_s += 1
            if drop_g or drop_s:
                log.info(">>> %s: %d produtos (%d brutos; -%d gênero/idade, "
                         "-%d tamanho 42/43)",
                         name, len(kept), len(products), drop_g, drop_s)
            else:
                log.info(">>> %s: %d produtos", name, len(kept))
            all_products.extend(kept)
            with connect(args.db) as conn:
                record_source_run(
                    conn, run_id=run_id, source=name, started_at=source_started,
                    status="success", raw_count=len(products), kept_count=len(kept),
                    drop_gender=drop_g, drop_size=drop_s,
                )
        except Exception:  # noqa: BLE001
            log.exception(">>> %s: falhou", name)
            failed.append(name)
            with connect(args.db) as conn:
                record_source_run(
                    conn, run_id=run_id, source=name, started_at=source_started,
                    status="failed", error="Falha durante scraping; consulte logs.",
                )

    counters = {}
    status = "success"
    if failed and all_products:
        status = "partial"
    elif failed and not all_products:
        status = "failed"
    with connect(args.db) as conn:
        if all_products:
            counters = record_run(conn, all_products, run_id=run_id)
        finish_run(conn, run_id, status=status, error=", ".join(failed) if failed else None)
    return all_products, failed, counters, run_id


def cmd_run(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    # Janela de detecção:
    # - alert: últimos 10s (= o que foi inserido nesta execução)
    # - digest: últimas 24h (consolida o dia inteiro de mudanças)
    if args.mode == "digest":
        cutoff_dt = now - timedelta(hours=args.digest_hours)
    else:
        cutoff_dt = now - timedelta(seconds=10)
    cutoff_iso = cutoff_dt.isoformat(timespec="seconds")

    all_products, failed, counters, run_id = _scrape_and_persist(args)
    if not all_products:
        log.warning("Nenhum produto coletado. Encerrando.")
        return 1 if failed else 0

    with connect(args.db) as conn:
        changes = find_changes(conn, cutoff_iso)

    log.info(
        "Resumo run_id=%s: %d novos produtos, %d atualizados, %d quedas, "
        "%d duplicados, %d novas promo, %d acabaram, %d enfraqueceram, %d subiram",
        run_id,
        counters["new"], counters["updated"], counters["price_drop"],
        counters.get("duplicates", 0),
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
                send_alert(changes, dry_run=args.dry_run_telegram)
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
    all_products, failed, counters, run_id = _scrape_and_persist(args)
    if not all_products:
        log.warning("Nenhum produto coletado. Encerrando.")
        return 1 if failed else 0

    with connect(args.db) as conn:
        changes = snapshot_promotions(conn)

    total = len(changes["new_promo"])
    log.info(
        "Snapshot run_id=%s: %d novos produtos, %d atualizados; %d em promoção agora.",
        run_id, counters.get("new", 0), counters.get("updated", 0), total,
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
    from .filters import should_keep

    with connect(args.db) as conn:
        rows = list(conn.execute(
            """
            SELECT p.source, p.sku, p.name, h.sizes
              FROM products p
              JOIN price_history h
                ON h.source = p.source AND h.sku = p.sku
               AND h.observed_at = (
                   SELECT MAX(observed_at) FROM price_history
                    WHERE source = p.source AND sku = p.sku
               )
            """
        ))
        to_drop: list[tuple[str, str, str, str]] = []  # source, sku, name, reason
        for r in rows:
            sizes = (r["sizes"] or "").split(",") if r["sizes"] else ()
            keep, reason = should_keep(r["name"] or "", sizes)
            if not keep:
                to_drop.append((r["source"], r["sku"], r["name"] or "", reason))

        if not to_drop:
            print("DB já está limpo — nenhum produto a remover.")
            return 0

        by_src = Counter(d[0] for d in to_drop)
        by_reason = Counter(d[3] for d in to_drop)

        mode = "APLICANDO" if args.apply else "Dry-run (use --apply pra executar)"
        print(f"=== Purge — {mode} ===")
        print(f"Critérios: gênero/idade + tênis 42/43 (filtros.py)\n")
        print("Por source:")
        for s in sorted(by_src):
            g = sum(1 for d in to_drop if d[0]==s and d[3]=="gender")
            z = sum(1 for d in to_drop if d[0]==s and d[3]=="size")
            print(f"  {s:18} {by_src[s]:4} a remover  ({g} gênero, {z} tamanho)")
        print(f"\nTotal: {len(to_drop)} produtos "
              f"({by_reason.get('gender',0)} por gênero/idade, "
              f"{by_reason.get('size',0)} por tamanho)")

        print(f"\nAmostra ({min(10, len(to_drop))} primeiros):")
        for src, sku, name, reason in to_drop[:10]:
            print(f"  [{src:14}] ({reason:6}) {name[:70]}")

        # Contar observações em cascata
        obs = conn.execute(
            "SELECT COUNT(*) FROM price_history h WHERE EXISTS ("
            "  SELECT 1 FROM products p WHERE p.source=h.source AND p.sku=h.sku)"
        ).fetchone()[0]
        target_obs = sum(
            conn.execute("SELECT COUNT(*) FROM price_history WHERE source=? AND sku=?",
                         (s, k)).fetchone()[0]
            for s, k, _, _ in to_drop
        )
        print(f"\nObservações em price_history a remover em cascata: {target_obs} "
              f"(de {obs} total).")

        if not args.apply:
            print("\nDry-run: nada foi modificado. Rode novamente com --apply pra deletar.")
            return 0

        # Apply: transação única
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            for src, sku, _, _ in to_drop:
                cur.execute("DELETE FROM price_history WHERE source=? AND sku=?", (src, sku))
                cur.execute("DELETE FROM products WHERE source=? AND sku=?", (src, sku))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"\n✔ Removidos {len(to_drop)} produtos e {target_obs} observações.")
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


def cmd_status(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        rows = latest_source_runs(conn)
    if not rows:
        print("Nenhuma execução registrada ainda.")
        return 0
    print("=== Última execução por fonte ===")
    for r in rows:
        print(
            f"  [{r['source']:26}] {r['status']:8} "
            f"brutos={r['raw_count']:5} mantidos={r['kept_count']:5} "
            f"início={r['started_at']} fim={r['finished_at'] or '—'}"
        )
        if r["error"]:
            print(f"    erro: {r['error']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ous-monitor")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="caminho do SQLite (default: data/prices.db)")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV, help="caminho do .env (default: ./.env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="roda scrapers e detecta mudanças")
    p_run.add_argument(
        "--sources", nargs="+", choices=source_keys(),
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
        "--sources", nargs="+", choices=source_keys(),
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

    p_status = sub.add_parser("status", help="mostra a última execução registrada por fonte")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    n = load_dotenv(args.env)
    if n:
        log.debug("dotenv: %d variáveis carregadas de %s", n, args.env)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
