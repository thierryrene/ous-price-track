from __future__ import annotations

import unittest

from ous_monitor.bot.callbacks import (
    MAX_CALLBACK_DATA_BYTES,
    decode_to_legacy,
    encode,
)


class BotCallbackCodecTests(unittest.TestCase):
    def test_round_trip_to_existing_router_format(self):
        data = encode("filter", "netshoes_adidas_originals", "disc", "50")

        self.assertEqual(
            decode_to_legacy(data),
            "filter:netshoes_adidas_originals:disc:50",
        )
        self.assertLessEqual(len(data.encode("utf-8")), MAX_CALLBACK_DATA_BYTES)

    def test_legacy_messages_remain_clickable(self):
        self.assertEqual(decode_to_legacy("stores:menu"), "stores:menu")

    def test_rejects_separator_and_oversized_payloads(self):
        with self.assertRaises(ValueError):
            encode("filter.bad")
        with self.assertRaises(ValueError):
            encode("x", "a" * 64)


if __name__ == "__main__":
    unittest.main()
