from __future__ import annotations

import unittest
from unittest.mock import patch

from ous_monitor.categories import categorize
from ous_monitor.notifier import build_summary, send_alert, send_digest


def row(name: str, price: float, list_price: float | None, url: str = "https://e.test/x") -> dict:
    return {
        "name": name, "price": price, "list_price": list_price, "url": url,
        "source": "test", "image": None,
        "prev_price": None, "prev_list_price": None,
        "sizes": None, "stock_qty": None,
    }


class CategorizeTest(unittest.TestCase):
    def test_buckets(self) -> None:
        cases = {
            "Tênis Adidas Forum Low": "tenis",
            "Chinelo Slide ÖUS": "tenis",
            "Camiseta Logo BaW": "vestuario",
            "Calça Cargo": "vestuario",
            "Moletom Canguru": "agasalhos",      # antes de vestuário
            "Jaqueta Corta Vento": "agasalhos",
            "Boné Trucker": "acessorios",
            "Meia Cano Alto": "acessorios",       # meia → acessórios, não vestuário
            "Mochila Skate": "acessorios",
            "Camisa do Flamengo Torcedor": "camisas_time",
            "Skate Shape Maple": "outros",
        }
        for name, expected in cases.items():
            self.assertEqual(categorize(name), expected, name)

    def test_accent_insensitive(self) -> None:
        self.assertEqual(categorize("TENIS sem acento"), "tenis")
        self.assertEqual(categorize("Óculos de Sol"), "acessorios")


class BuildSummaryTest(unittest.TestCase):
    def _changes(self):
        return {
            "new_promo": [
                row("Tênis Adidas Forum", 189, 499),
                row("Tênis Nike SB", 249, 519),
                row("Tênis ÖUS Hoka", 224, 430),
                row("Camiseta Logo", 89, 149),
                row("Boné Trucker", 72, 120),
            ],
            "weaker": [row("Moletom X", 200, 240)],
            "price_up": [row("Calça Y", 110, 150)],
            "ended": [row("Bota Z", 300, 300)],
        }

    def test_groups_and_orders_by_discount(self) -> None:
        msgs = build_summary(self._changes(), period_label="hoje")
        body = "\n".join(msgs)
        self.assertIn("5 promoção(ões) nova(s) — hoje", body)
        self.assertIn("TÊNIS/CALÇADOS (3)", body)
        self.assertIn("VESTUÁRIO (1)", body)
        self.assertIn("ACESSÓRIOS (1)", body)
        # Maior desconto primeiro dentro do grupo de tênis:
        # Adidas -62%, Hoka -48%, Nike -52% → Adidas, Nike, Hoka
        forum = body.index("Adidas Forum")
        nike = body.index("Nike SB")
        hoka = body.index("Hoka")
        self.assertLess(forum, nike)
        self.assertLess(nike, hoka)
        # Rodapé compacto weaker/price_up; ended omitido.
        self.assertIn("Ficou pior", body)
        self.assertIn("📉 1 desconto encolheu", body)
        self.assertIn("📈 1 subiu", body)
        self.assertNotIn("Bota Z", body)

    def test_per_group_cap(self) -> None:
        changes = {"new_promo": [row(f"Tênis {i}", 100 + i, 300) for i in range(20)]}
        with patch.dict("os.environ", {"SUMMARY_PER_GROUP": "5"}):
            body = "\n".join(build_summary(changes, period_label="hoje"))
        self.assertIn("…+15 mais", body)

    def test_send_alert_auto_summarizes_above_threshold(self) -> None:
        changes = {"new_promo": [row(f"Tênis {i}", 100, 300) for i in range(20)],
                   "ended": [], "weaker": [], "price_up": []}
        with patch.dict("os.environ", {"SUMMARY_THRESHOLD": "10"}):
            with patch("ous_monitor.notifier.build_summary",
                       wraps=build_summary) as spy:
                send_alert(changes, dry_run=True)
        spy.assert_called_once()

    def test_send_alert_keeps_rich_below_threshold(self) -> None:
        changes = {"new_promo": [row("Tênis Único", 100, 300)],
                   "ended": [], "weaker": [], "price_up": []}
        with patch.dict("os.environ", {"SUMMARY_THRESHOLD": "10"}):
            with patch("ous_monitor.notifier.build_summary") as spy:
                send_alert(changes, dry_run=True)
        spy.assert_not_called()

    def test_send_digest_summarizes_by_default(self) -> None:
        changes = {"new_promo": [row("Tênis Único", 100, 300)],
                   "ended": [], "weaker": [], "price_up": []}
        with patch("ous_monitor.notifier.build_summary", wraps=build_summary) as spy:
            send_digest(changes, period_label="hoje", dry_run=True)
        spy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
