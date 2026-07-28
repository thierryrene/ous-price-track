from __future__ import annotations

import unittest

from ous_monitor.filters import is_tenis, should_keep


class FilterTests(unittest.TestCase):
    def test_gender_blocks_feminine_and_kids_terms(self):
        self.assertEqual(should_keep("Tênis Adidas Feminino", ["42"]), (False, "gender"))
        self.assertEqual(should_keep("Chuteira Umbro Junior", ["42"]), (False, "gender"))

    def test_tenis_size_filter_requires_wanted_size_when_sizes_exist(self):
        self.assertTrue(is_tenis("Tênis Umbro Speciali"))
        self.assertEqual(should_keep("Tênis Umbro Speciali", ["40", "41"]), (False, "size"))
        self.assertEqual(should_keep("Tênis Umbro Speciali", ["41", "42"]), (True, ""))

    def test_tenis_without_sizes_is_rejected(self):
        self.assertEqual(should_keep("Tênis OUS Imigrante", []), (False, "size"))

    def test_clothing_requires_m_g_or_gg(self):
        self.assertEqual(should_keep("Camiseta OUS Masculina", ["P"]), (False, "size"))
        self.assertEqual(should_keep("Camiseta OUS Masculina", ["P", "M"]), (True, ""))
        self.assertEqual(should_keep("Moletom OUS Unissex", ["GG"]), (True, ""))

    def test_accessory_does_not_require_size(self):
        self.assertEqual(should_keep("Boné OUS Masculino", []), (True, ""))


if __name__ == "__main__":
    unittest.main()
